# pip install requests beautifulsoup4 lxml

import requests, json, time, re, csv
from urllib.parse import urljoin, urlparse
from bs4 import BeautifulSoup

INDEX_URL = "https://www.allnigerianrecipes.com/other/sitemap/"
BASE_DOMAIN = "www.allnigerianrecipes.com"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
}

def same_domain(url, domain=BASE_DOMAIN):
    return urlparse(url).netloc.lower() == domain.lower()

def likely_recipe(url):
    path = urlparse(url).path.lower()
    # tune as needed
    keywords = ["recipe", "how-to", "soups", "stews", "rice", "beans", "snacks", "drinks"]
    return any(k in path for k in keywords) and not path.endswith((".xml", ".pdf", ".zip"))

def get_all_links_from_index(index_url):
    s = requests.Session(); s.headers.update(HEADERS)
    r = s.get(index_url, timeout=20); r.raise_for_status()
    soup = BeautifulSoup(r.content, "html.parser")

    links = []
    for a in soup.select("a[href]"):
        href = a["href"]
        full = urljoin(index_url, href)
        if same_domain(full):
            links.append(full)

    # stable de-dupe (preserve order)
    uniq, seen = [], set()
    for u in links:
        if u not in seen:
            seen.add(u)
            uniq.append(u)
    return uniq

def extract_recipe_jsonld(soup):
    def to_list(x): return x if isinstance(x, list) else [x]
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "{}")
        except Exception:
            continue
        blobs = data.get("@graph", [data]) if isinstance(data, dict) else (data if isinstance(data, list) else [])
        for node in blobs:
            if isinstance(node, dict):
                t = node.get("@type")
                if t == "Recipe" or (isinstance(t, list) and "Recipe" in t):
                    title = (node.get("name") or "").strip()
                    ings_raw = node.get("recipeIngredient") or []
                    instr_raw = node.get("recipeInstructions") or []
                    ingredients = []
                    for ing in to_list(ings_raw):
                        if isinstance(ing, dict):
                            ingredients.append((ing.get("name") or ing.get("text") or "").strip())
                        else:
                            ingredients.append(str(ing).strip())
                    steps = []
                    for step in to_list(instr_raw):
                        if isinstance(step, dict):
                            txt = (step.get("text") or step.get("name") or "").strip()
                            if txt: steps.append(txt)
                        else:
                            s = str(step).strip()
                            if s: steps.append(s)
                    return {
                        "title": title or None,
                        "ingredients": [i for i in ingredients if i],
                        "instructions": [s for s in steps if s],
                        "prep_time": node.get("prepTime"),
                        "cook_time": node.get("cookTime"),
                        "total_time": node.get("totalTime"),
                        "servings": node.get("recipeYield"),
                    }
    return None

def fallback_extract(soup):
    title_tag = soup.find("h1") or soup.find("title")
    title = (title_tag.get_text(strip=True) if title_tag else None)

    # microdata
    micro = soup.find(attrs={"itemtype": re.compile("schema.org/Recipe", re.I)})
    if micro:
        ings = [e.get_text(strip=True) for e in micro.select("[itemprop='recipeIngredient']")]
        steps = [e.get_text(strip=True) for e in micro.select("[itemprop='recipeInstructions'] li, [itemprop='recipeInstructions'] p")]
        if not steps:
            block = micro.select_one("[itemprop='recipeInstructions']")
            if block:
                steps = [s.strip() for s in block.get_text("\n").split("\n") if s.strip()]
        return {"title": title, "ingredients": ings, "instructions": steps}

    # very light heuristic: first UL as ingredients, first OL as instructions
    content = soup.select_one(".entry-content, article, main")
    ingredients, instructions = [], []
    if content:
        ul = content.find("ul")
        ol = content.find("ol")
        if ul: ingredients = [li.get_text(strip=True) for li in ul.select("li")]
        if ol: instructions = [li.get_text(strip=True) for li in ol.select("li")]
    return {"title": title, "ingredients": ingredients, "instructions": instructions}

def fetch_and_parse(url, session):
    r = session.get(url, timeout=25, headers=HEADERS)
    r.raise_for_status()
    return BeautifulSoup(r.content, "lxml")

def scrape_recipe(url, session):
    try:
        soup = fetch_and_parse(url, session)
    except Exception as e:
        print(f"[skip] fetch failed {url}: {e}")
        return None
    data = extract_recipe_jsonld(soup) or fallback_extract(soup)
    title = (data.get("title") or "").strip()
    if not title:
        return None
    return {
        "title": title,
        "url": url,
        "source": BASE_DOMAIN,
        "ingredients": data.get("ingredients") or [],
        "instructions": data.get("instructions") or [],
        "prep_time": data.get("prep_time"),
        "cook_time": data.get("cook_time"),
        "total_time": data.get("total_time"),
        "servings": data.get("servings"),
    }

def save_json(rows, path="nigerian_recipes.json"):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(rows, f, indent=2, ensure_ascii=False)

def save_csv(rows, path="nigerian_recipes.csv"):
    fields = ["title","url","source","prep_time","cook_time","total_time","servings","ingredients","instructions"]
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields); w.writeheader()
        for r in rows:
            row = r.copy()
            row["ingredients"] = " | ".join(row["ingredients"])
            row["instructions"] = " | ".join(row["instructions"])
            w.writerow(row)

def main():
    print("[1/3] Reading link index page…")
    all_links = get_all_links_from_index(INDEX_URL)
    same_site = [u for u in all_links if same_domain(u)]
    recipe_urls = [u for u in same_site if likely_recipe(u)]

    # stable dedupe again just in case
    seen, ordered = set(), []
    for u in recipe_urls:
        if u not in seen:
            seen.add(u); ordered.append(u)

    print(f"[i] Candidate recipe URLs: {len(ordered)}")

    print("[2/3] Scraping recipes…")
    session = requests.Session(); session.headers.update(HEADERS)
    out = []
    for i, url in enumerate(ordered, 1):
        item = scrape_recipe(url, session)
        if item:
            out.append(item)
        if i % 25 == 0:
            print(f"  …processed {i}/{len(ordered)}")
        time.sleep(0.4)  # polite

    print(f"[i] Extracted {len(out)} recipes.")

    print("[3/3] Saving…")
    save_json(out)
    save_csv(out)
    print("[ok] Done.")

if __name__ == "__main__":
    main()
