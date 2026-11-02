#!/usr/bin/env python3
"""
Mirror a JavaScript-driven page for offline use and pull out the data it runs on.

Setup (once):
    pip install playwright json5
    playwright install chromium

Run:
    python scrape_derm_engine.py                  # the dermatology engine page
    python scrape_derm_engine.py <url> --out out  # any other page

Writes:
    out/site/            offline copy: the HTML plus every file it loaded (JS, CSS, fonts, JSON, images),
                         the knowledge-API answers for ALL presentations, and offline-shim.js
    out/data/            each dataset found, one JSON file per dataset
    out/manifest.json    what was saved, where each dataset came from, and what could NOT be resolved

This engine loads one presentation per pick from a knowledge API and sends the entered findings to the
server to be scored. The script asks the page itself to load every presentation, and offline-shim.js
answers those API calls from the saved files. Scoring (POST .../assess) needs the server's engine
(derm_engine.js): until that is installed, the page shows its own "Not connected" banner instead of a ranking.

The page is opened in a real browser, so data that is inline, in a separate .js/.json file, or fetched
at runtime is all captured. Read manifest.json["caveats"] before trusting the copy.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
from datetime import datetime, timezone
from html import unescape
from pathlib import Path
from urllib.parse import quote, unquote, urljoin, urlparse

from playwright.sync_api import sync_playwright

try:
    import json5  # reads JS object literals: unquoted keys, single quotes, trailing commas, comments
except ImportError:
    json5 = None

DEFAULT_URL = "https://medconsults.ai/dermatology-differential-engine.html"
MIN_CHARS = 500  # objects/literals smaller than this are not treated as datasets (they are still mirrored)

# trackers: not needed offline, and they should not fire from inside the app
SKIP = re.compile(r"google-analytics|googletagmanager|cloudflareinsights|plausible\.io|clarity\.ms|hotjar|doubleclick", re.I)

EXT_BY_TYPE = {
    "text/css": "css", "text/javascript": "js", "application/javascript": "js", "application/json": "json",
    "image/svg+xml": "svg", "image/png": "png", "image/jpeg": "jpg", "image/webp": "webp",
    "font/woff2": "woff2", "font/woff": "woff", "text/html": "html",
}
TEXT_EXT = {"html", "htm", "css", "js", "mjs", "json", "svg", "txt", "csv"}
REWRITE_EXT = {"html", "htm", "css", "js", "mjs"}

# quoted strings that look like file paths, and CSS url(...)
REF = re.compile(
    r"""["'`]([^"'`\s<>(){}\\$]+\.(?:json|js|mjs|css|csv|svg|png|jpe?g|webp|gif|woff2?|ttf|txt)(?:\?[^"'`\s<>\\$]*)?)["'`]""",
    re.I,
)
CSS_URL = re.compile(r"""url\(\s*['"]?([^'")\s]+)['"]?\s*\)""", re.I)
# data declared as: const X = {..} | let X = [..] | var X = .. | window.X = ..
DECL = re.compile(r"(?<![\w$.])(?:(?:window|globalThis)\.|(?:const|let|var)\s+)([A-Za-z_$][\w$]*)\s*=\s*(?=[\[{])")
IGNORE_VARS = {"DERM"}  # the page's runtime cache of API replies (bootstrap + the last presentation loaded)

# Injected as the first script of the offline page. It answers the page's calls to /api/knowledge/derm/...
# from the files saved in the mirror, whatever host the page believes the API is on.
SHIM_JS = r"""/* offline-shim.js - answers the page's knowledge-API calls from files saved next to the page */
(function () {
  "use strict";
  var PREFIX = "/api/knowledge/derm";
  var LOCAL = "__LOCAL__";            /* where the scraper saved the API replies */
  var realFetch = window.fetch.bind(window);
  var cache = {};
  function json(obj, status) {
    return new Response(JSON.stringify(obj), { status: status || 200, headers: { "Content-Type": "application/json" } });
  }
  function load(path) {
    if (!cache[path]) {
      cache[path] = realFetch(LOCAL + path + ".json", { cache: "no-store" }).then(function (r) {
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.json();
      }).catch(function (e) { delete cache[path]; throw e; });
    }
    return cache[path];
  }
  window.fetch = function (input, init) {
    var raw = typeof input === "string" ? input : (input && input.url) || String(input);
    var u;
    try { u = new URL(raw, location.href); } catch (e) { return realFetch(input, init); }
    if (u.pathname.indexOf(PREFIX) !== 0) return realFetch(input, init);
    var path = u.pathname.slice(PREFIX.length);                      /* /bootstrap, /presentation/<id>, /assess */
    var method = String((init && init.method) || (input && input.method) || "GET").toUpperCase();
    if (method === "GET") {
      return load(path).then(function (d) { return json(d); },
                             function () { return json({ error: "not available offline" }, 404); });
    }
    if (method === "POST" && path === "/assess") {
      /* WIRE-UP POINT: scoring runs on the server (derm_engine.js). Until that engine is installed here as
         window.DERM_ASSESS(presentation, ctx, bootstrap) -> response, answer 503 so the page says "Not connected". */
      if (typeof window.DERM_ASSESS !== "function") return Promise.resolve(json({ error: "offline scoring engine not installed" }, 503));
      var ctx;
      try { ctx = (JSON.parse(init.body) || {}).ctx || {}; } catch (e) { return Promise.resolve(json({ error: "bad request" }, 400)); }
      return Promise.all([load("/presentation/" + encodeURIComponent(ctx.presId)), load("/bootstrap")])
        .then(function (a) { return window.DERM_ASSESS(a[0], ctx, a[1]); })
        .then(function (R) {
          if (init.signal && init.signal.aborted) throw new DOMException("Aborted", "AbortError");
          return json(R);
        }, function (e) {
          if (e && e.name === "AbortError") throw e;
          return json({ error: String(e) }, 500);
        });
    }
    return Promise.resolve(json({ error: "not available offline" }, 404));
  };
})();
"""

# fetch(x) / import(x) where x is not a plain string literal -> cannot be resolved statically
DYNAMIC = re.compile(r"\b(?:fetch|import)\(\s*(?![\"'])([^)]{1,80})\)")


def origin_key(url: str) -> str:
    """medconsults.ai and www.medconsults.ai count as one site; the port is kept."""
    n = urlparse(url).netloc.lower()
    return n[4:] if n.startswith("www.") else n


def looks_textual(url: str, ctype: str) -> bool:
    ext = urlparse(url).path.rpartition(".")[2].lower()
    return ext in TEXT_EXT or ctype.startswith("text/") or "json" in ctype or "javascript" in ctype


def local_path(url: str, ctype: str, base_key: str) -> str:
    """Where a captured URL is stored inside out/site/. Same-site files keep their URL path."""
    p = urlparse(url)
    same = origin_key(url) == base_key
    path = unquote(p.path) or "/"
    if path.endswith("/"):
        path += "index.html"
    folder, _, name = path.rpartition("/")
    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, ext = name, ""
        if not same:  # same-site files keep their exact URL so runtime requests still resolve
            ext = EXT_BY_TYPE.get(ctype.split(";")[0].strip().lower(), "")
    if p.query and not same:  # keep fonts.googleapis.com/css2?family=A and ?family=B apart
        stem += "__" + hashlib.md5(p.query.encode()).hexdigest()[:8]
    rel = f"{folder}/{stem}{'.' + ext if ext else ''}".lstrip("/")
    if not same:
        rel = f"external/{p.netloc}/{rel}"  # not "_ext": Android's build silently drops asset folders starting with "_"
    return re.sub(r'[<>:"|?*]', "_", rel)


def extract_literal(src: str, start: int):
    """src[start] is '[' or '{'. Return the balanced literal, skipping strings and comments."""
    close = {"[": "]", "{": "}", "(": ")"}
    stack, i, n = [], start, len(src)
    while i < n:
        c = src[i]
        if c in "\"'`":
            i += 1
            while i < n and src[i] != c:
                i += 2 if src[i] == "\\" else 1
        elif src.startswith("//", i):
            j = src.find("\n", i)
            i = n if j < 0 else j
        elif src.startswith("/*", i):
            j = src.find("*/", i + 2)
            i = n if j < 0 else j + 1
        elif c in close:
            stack.append(close[c])
        elif c in "]})":
            if not stack or stack.pop() != c:
                return None
            if not stack:
                return src[start:i + 1]
        i += 1
    return None


def parse_literal(text: str):
    for load in (json.loads, json5.loads if json5 else None):
        if load is None:
            continue
        try:
            return load(text)
        except Exception:
            pass
    return None


def shape(v) -> str:
    if isinstance(v, list):
        return f"array[{len(v)}]"
    if isinstance(v, dict):
        keys = list(v)
        return "object{" + ", ".join(map(str, keys[:12])) + (", ..." if len(keys) > 12 else "") + "}"
    return type(v).__name__


def load_all_presentations(page, captured: dict, caveats: list) -> list:
    """This engine fetches ONE presentation per pick. Ask the page itself to load every one listed in its
    index, so the requests are exactly the ones the page makes. Retries with back-off (the API rate-limits)."""
    ids = page.evaluate("typeof INDEX !== 'undefined' && Array.isArray(INDEX) ? INDEX.map(p => p.id) : []")
    if not ids or not page.evaluate("typeof loadPresentation === 'function'"):
        caveats.append("INDEX / loadPresentation() not found on the page: only the default presentation was captured.")
        return []

    def have(i: str) -> bool:
        return any(urlparse(u).path.endswith("/presentation/" + quote(i, safe="")) for u in captured)

    errors: dict[str, str] = {}
    missing = [i for i in ids if not have(i)]
    for attempt in range(4):
        for i in list(missing):
            try:
                page.evaluate("(id) => loadPresentation(id)", i)
            except Exception as e:  # e.g. HTTP 429
                errors[i] = str(e).splitlines()[0][:100]
            page.wait_for_timeout(300)  # let the response handler store the body
            if have(i):
                missing.remove(i)
            page.wait_for_timeout(700)
        if not missing:
            break
        page.wait_for_timeout(5000 * (attempt + 1))
    for i in missing:
        caveats.append(f"Presentation '{i}' could not be fetched ({errors.get(i, 'no response captured')}): "
                       "it is NOT in the offline copy.")
    return ids


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("url", nargs="?", default=DEFAULT_URL)
    ap.add_argument("--out", default="out")
    ap.add_argument("--min-chars", type=int, default=MIN_CHARS, help="smallest object treated as a dataset (default %(default)s)")
    ap.add_argument("--browser-path", help="Chromium/Chrome executable to use instead of Playwright's own")
    a = ap.parse_args()

    out = Path(a.out)
    for d in ("site", "data"):
        shutil.rmtree(out / d, ignore_errors=True)
        (out / d).mkdir(parents=True)

    base_key = origin_key(a.url)
    captured: dict[str, tuple[str, bytes]] = {}  # url -> (content-type, body)
    caveats: list[str] = []
    datasets: list[dict] = []
    seen_hashes: set[str] = set()
    used_names: set[str] = set()

    def add_dataset(name: str, value, how: str, origin: str) -> None:
        blob = json.dumps(value, sort_keys=True, ensure_ascii=False)
        h = hashlib.md5(blob.encode()).hexdigest()
        if len(blob) < a.min_chars or h in seen_hashes:
            return
        seen_hashes.add(h)
        base = re.sub(r"[^\w.-]+", "_", name) or "data"
        fname, k = base, 2
        while fname in used_names:
            fname, k = f"{base}_{k}", k + 1
        used_names.add(fname)
        (out / "data" / f"{fname}.json").write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding="utf-8")
        datasets.append({"file": f"data/{fname}.json", "name": name, "found_by": how, "origin": origin,
                         "chars": len(blob), "shape": shape(value)})

    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=a.browser_path) if a.browser_path else pw.chromium.launch()
        ctx = browser.new_context(viewport={"width": 1280, "height": 900})
        ctx.route(SKIP, lambda route: route.abort())
        page = ctx.new_page()

        def on_response(resp) -> None:
            if resp.status != 200 or resp.request.method != "GET":
                return
            try:
                captured[resp.url.split("#")[0]] = (resp.headers.get("content-type", ""), resp.body())
            except Exception:
                pass  # redirects / evicted bodies

        page.on("response", on_response)
        main_resp = page.goto(a.url, wait_until="load", timeout=60_000)
        try:
            page.wait_for_load_state("networkidle", timeout=15_000)
        except Exception:
            caveats.append("Network did not go idle within 15 s; late requests may be missing.")
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(1500)
        page_url = main_resp.url.split("#")[0]
        captured.setdefault(page_url, ("text/html", main_resp.body()))
        pres_ids = load_all_presentations(page, captured, caveats)

        # ---- 1. files that same-site code refers to but that did not load during the visit (lazy data, hover images)
        failed: set[str] = set()
        other_hosts: set[str] = set()
        budget = 400
        for _ in range(3):
            new: dict[str, tuple[str, bytes]] = {}
            for url, (ct, body) in list(captured.items()):
                is_css = urlparse(url).path.endswith(".css") or ct.startswith("text/css")
                if not looks_textual(url, ct) or not (origin_key(url) == base_key or is_css):
                    continue
                text = body.decode("utf-8", "ignore")
                base = url if urlparse(url).path.endswith(".css") else page_url
                for ref in [*REF.findall(text), *CSS_URL.findall(text)]:
                    if ref.startswith(("data:", "blob:", "#", "javascript:")):
                        continue
                    cand = urljoin(base, unescape(ref)).split("#")[0]
                    if cand in captured or cand in new or cand in failed:
                        continue
                    if origin_key(cand) != base_key and not is_css:  # code on another host is not followed ...
                        other_hosts.add(cand)
                        continue                                       # ... but everything a stylesheet lists is
                    if budget <= 0:
                        continue
                    budget -= 1
                    try:
                        r = ctx.request.get(cand, timeout=20_000)
                        rct = r.headers.get("content-type", "")
                        spa_fallback = rct.startswith("text/html") and not urlparse(cand).path.endswith((".html", ".htm", "/"))
                        if r.status == 200 and not spa_fallback:
                            new[cand] = (rct, r.body())
                        else:
                            failed.add(cand)
                    except Exception:
                        failed.add(cand)
                    if cand in failed and origin_key(cand) != base_key:
                        caveats.append(f"Listed by a stylesheet but could not be fetched: {cand}")
            if not new:
                break
            captured.update(new)
        for cand in sorted(other_hosts - set(captured)):
            caveats.append(f"Referenced by same-site code but hosted elsewhere and not mirrored: {cand}")

        # ---- 2. network calls whose target is computed at run time cannot be listed statically
        dyn_seen: set[tuple[str, str]] = set()
        for url, (ct, body) in captured.items():
            if origin_key(url) == base_key and looks_textual(url, ct):
                for m in DYNAMIC.finditer(body.decode("utf-8", "ignore")):
                    if (url, m.group(0)) not in dyn_seen and len(dyn_seen) < 20:
                        dyn_seen.add((url, m.group(0)))
                        caveats.append(f"Computed network call {m.group(0)!r} in {url}: if it loads data that was "
                                       "not captured, trigger that path in the page and re-run, or add the file by hand.")

        # ---- 3. datasets
        inline = page.evaluate(
            "[...document.scripts].filter(s => !s.src).map(s => ({id: s.id, type: s.type, text: s.textContent}))")
        sources: list[tuple[str, str]] = []
        for i, s in enumerate(inline, 1):
            label = f"inline <script{' id=' + s['id'] if s['id'] else ''}> #{i}"
            if "json" in (s["type"] or ""):
                val = parse_literal(s["text"])
                if val is None:
                    caveats.append(f"{label}: JSON script tag could not be parsed.")
                else:
                    add_dataset(s["id"] or f"inline_json_{i}", val, "inline JSON script tag", label)
            else:
                sources.append((label, s["text"]))
        for url, (ct, body) in captured.items():
            if origin_key(url) == base_key and (urlparse(url).path.endswith((".js", ".mjs")) or "javascript" in ct):
                sources.append((url, body.decode("utf-8", "ignore")))

        runtime_cache: dict = {}
        for origin, src in sources:
            for m in DECL.finditer(src):
                lit = extract_literal(src, m.end())
                if not lit or len(lit) < 20:
                    continue
                name = m.group(1)
                if name in IGNORE_VARS:
                    continue
                # the page's own value (resolves spreads, computed fields, function calls) ...
                if name not in runtime_cache:  # one lookup per variable name, however often it is declared
                    runtime_cache[name] = page.evaluate(
                        f"(() => {{ try {{ return JSON.parse(JSON.stringify({name})) }} catch (e) {{ return null }} }})()")
                val = runtime_cache[name]
                how = "runtime value of the page's own variable"
                if val is None:  # ... else the literal in the source (module-scoped or function-local data)
                    if len(lit) < a.min_chars:
                        continue
                    val, how = parse_literal(lit), "static parse of the source literal"
                if val is None:
                    safe = re.sub(r"[^\w.-]+", "_", name)
                    (out / "data" / f"{safe}.unparsed.txt").write_text(lit, encoding="utf-8")
                    caveats.append(f"{name} in {origin}: too dynamic to parse; raw source saved as data/{safe}.unparsed.txt")
                    continue
                add_dataset(name, val, how, origin)

        for url, (ct, body) in captured.items():
            if "json" in ct or urlparse(url).path.endswith(".json"):
                try:
                    val = json.loads(body.decode("utf-8-sig"))
                except Exception:
                    caveats.append(f"{url}: JSON response could not be parsed.")
                    continue
                parts = [x for x in unquote(urlparse(url).path).split("/") if x]
                parts[-1] = Path(parts[-1]).stem if parts else "response"
                add_dataset("_".join(parts[-2:]) or "response", val, "network response", url)

        blank = ctx.new_page()
        blank.goto("about:blank")
        base_globals = set(blank.evaluate("Object.getOwnPropertyNames(window)"))
        blank.close()
        extra = [g for g in page.evaluate("Object.getOwnPropertyNames(window)") if g not in base_globals]
        dump = page.evaluate(
            "(names) => { const out = {}; for (const n of names) { try { const v = window[n]; "
            "if (!v || typeof v !== 'object') continue; const s = JSON.stringify(v); "
            "if (s && s.length >= MIN_CHARS) out[n] = JSON.parse(s); } catch (e) {} } return out; }"
            .replace("MIN_CHARS", str(a.min_chars)),
            extra,
        )
        for name, val in dump.items():
            add_dataset(name, val, "global variable on window", "page runtime")

        browser.close()

    # ---- 4. write the offline mirror; cross-site URLs are repointed at /external/<host>/..., root-relative
    paths = {u: local_path(u, ct, base_key) for u, (ct, _) in captured.items()}
    repl: dict[str, str] = {}
    for u, rel in paths.items():
        for form in (u, u.split(":", 1)[1]):  # absolute and protocol-relative spellings
            repl[form] = repl[form.replace("&", "&amp;")] = "/" + rel
    rewrite = re.compile("(?:" + "|".join(re.escape(k) for k in sorted(repl, key=len, reverse=True)) + r")(?![\w/%.-])")

    mirrored = []
    for u, (ct, body) in captured.items():
        rel = paths[u]
        leaf = rel.rsplit("/", 1)[-1]
        ext = leaf.rpartition(".")[2].lower() if "." in leaf else ""
        if ext in REWRITE_EXT:
            try:
                text = rewrite.sub(lambda m: repl[m.group(0)], body.decode("utf-8"))
                if ext in ("html", "htm"):
                    text = re.sub(r'\sintegrity="[^"]*"', "", text)  # rewritten files no longer match their SRI hashes
                body = text.encode("utf-8")
            except UnicodeDecodeError:
                caveats.append(f"{u}: not UTF-8, saved without URL rewriting.")
        dest = out / "site" / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(body)
        mirrored.append({"url": u, "file": f"site/{rel}", "bytes": len(body)})
    api_dir = next(("/" + rel.rsplit("/", 1)[0] for u, rel in paths.items()
                    if urlparse(u).path.endswith("/api/knowledge/derm/bootstrap")), None)
    if api_dir:
        (out / "site" / "offline-shim.js").write_text(SHIM_JS.replace("__LOCAL__", api_dir), encoding="utf-8")
        main_file = out / "site" / paths[page_url]
        html_text, n = re.subn(r"(<head[^>]*>)", r'\1<script src="offline-shim.js"></script>',
                               main_file.read_text(encoding="utf-8"), count=1, flags=re.I)
        if n:
            main_file.write_text(html_text, encoding="utf-8")
        else:
            caveats.append("No <head> tag found: offline-shim.js was written but NOT linked into the page.")
    else:
        caveats.append("No knowledge-API calls were captured: offline-shim.js was not created.")
    if paths[page_url] != "index.html":
        shutil.copyfile(out / "site" / paths[page_url], out / "site" / "index.html")

    manifest = {
        "page": a.url,
        "scraped_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "datasets": datasets,
        "caveats": caveats,
        "mirrored": mirrored,
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"Mirrored {len(mirrored)} files -> {out / 'site'}  (entry: {paths[page_url]}, also index.html)")
    print(f"Presentations on the page's index: {len(pres_ids)}; API replies mirrored: "
          f"{sum(1 for u in captured if '/api/knowledge/derm/presentation/' in u)}")
    print(f"Datasets found: {len(datasets)} -> {out / 'data'}")
    for d in datasets:
        print(f"  {d['file']:<40} {d['chars']:>9,} chars  {d['shape'][:60]}  [{d['found_by']}]")
    print(f"Caveats: {len(caveats)} (see manifest.json)")
    for c in caveats:
        print("  - " + c)


if __name__ == "__main__":
    main()
