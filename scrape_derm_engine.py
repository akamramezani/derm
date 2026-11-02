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
    out/site/            offline copy: the HTML plus every file it loaded (JS, CSS, fonts, JSON, images)
    out/data/            each dataset found, one JSON file per dataset
    out/manifest.json    what was saved, where each dataset came from, and what could NOT be resolved

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
from urllib.parse import unquote, urljoin, urlparse

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
        rel = f"_ext/{p.netloc}/{rel}"
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

        # ---- 1. files that same-site code refers to but that did not load during the visit (lazy data, hover images)
        failed: set[str] = set()
        other_hosts: set[str] = set()
        budget = 400
        for _ in range(3):
            new: dict[str, tuple[str, bytes]] = {}
            for url, (ct, body) in list(captured.items()):
                if origin_key(url) != base_key or not looks_textual(url, ct):
                    continue
                text = body.decode("utf-8", "ignore")
                base = url if urlparse(url).path.endswith(".css") else page_url
                for ref in [*REF.findall(text), *CSS_URL.findall(text)]:
                    if ref.startswith(("data:", "blob:", "#", "javascript:")):
                        continue
                    cand = urljoin(base, unescape(ref)).split("#")[0]
                    if cand in captured or cand in new or cand in failed:
                        continue
                    if origin_key(cand) != base_key:
                        other_hosts.add(cand)
                        continue
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
                add_dataset(Path(unquote(urlparse(url).path)).stem or "response", val, "network response", url)

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

    # ---- 4. write the offline mirror; cross-site URLs are repointed at /_ext/<host>/..., root-relative
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
    print(f"Datasets found: {len(datasets)} -> {out / 'data'}")
    for d in datasets:
        print(f"  {d['file']:<40} {d['chars']:>9,} chars  {d['shape'][:60]}  [{d['found_by']}]")
    print(f"Caveats: {len(caveats)} (see manifest.json)")
    for c in caveats:
        print("  - " + c)


if __name__ == "__main__":
    main()
