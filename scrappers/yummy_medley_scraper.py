import re
from typing import Iterable, Optional
from urllib.parse import urljoin, urlparse
from bs4 import BeautifulSoup
from embedder.hugging_face_embedder import HFEmbedder
from scrappers.base_scrapper import BaseRecipeScraper, RecipeDoc, clean, get_meta_image

class YummyMedleyScraper(BaseRecipeScraper):
    """Specifically targets WPRM (WP Recipe Maker) blocks on yummymedley.com"""

    # Only collect tags from the home page tag-cloud widget
    TAG_CLOUD_SELECTORS = [
        "#tag_cloud-4 .tagcloud a[href*='/tag/']",
        "div.widget_tag_cloud .tagcloud a[href*='/tag/']",
    ]

    # How we find post links on a tag page (grid cards & headers)
    POST_LINK_SELECTORS = [
        "#main ul.sp-grid li article .post-header a[href]",  # header link
        "#main ul.sp-grid li article .post-img a[href]",     # image link
        "#main article .post-header a[href]",                # fallback
    ]

    TAG_RE = re.compile(r"/tag/[^/]+/?$")

    def __init__(self, base_domain="www.yummymedley.com"):
        super().__init__(base_domain)

    # --- NEW: get tag URLs only from home page tag cloud ---
    def _discover_tag_urls_from_home(self) -> list[str]:
        soup = self.fetch_soup(self.base_url)
        tags = set()

        # prefer strict selector; gracefully fall back
        anchors = []
        for sel in self.TAG_CLOUD_SELECTORS:
            anchors = soup.select(sel)
            if anchors:
                break

        if not anchors:
            # final fallback: any /tag/... link on home page
            anchors = soup.find_all("a", href=self.TAG_RE)

        for a in anchors:
            href = a.get("href")
            if href:
                tags.add(urljoin(self.base_url, href))
        return sorted(tags)

    # --- NEW: extract post links from a single tag page soup ---
    def _extract_post_links_from_tag_page(self, soup: BeautifulSoup) -> set[str]:
        links = set()
        for sel in self.POST_LINK_SELECTORS:
            for a in soup.select(sel):
                href = a.get("href")
                if href:
                    links.add(urljoin(self.base_url, href))
            if links:
                break  # got some via this selector
        return links

    # --- helper: is this URL an article we should open? ---

    # --- REWRITTEN: discover only from home-page tag cloud, then crawl each tag ---
    def discover_urls(self) -> Iterable[str]:
        tags = self._discover_tag_urls_from_home()
        if not tags:
            # Safety: if tag cloud not found, bail early (or fallback to /recipes/)
            self.logger.warning("No tags discovered from home page tag cloud; falling back to /recipes/")
            tags = [urljoin(self.base_url, "/recipes/")]

        seen = set()
        for tag_url in tags:
            page = 1
            while page <= 20:  # hard cap to avoid runaway pagination
                url = tag_url if page == 1 else f"{tag_url.rstrip('/')}/page/{page}/"
                try:
                    soup = self.fetch_soup(url)
                except Exception as e:
                    self.logger.warning(f"[tag] fetch failed {url}: {e}")
                    break

                post_links = self._extract_post_links_from_tag_page(soup)
                if not post_links:
                    # no posts found -> stop paginating this tag
                    break

                for u in sorted(post_links):
                    if u not in seen and self._looks_like_article(u):
                        seen.add(u)
                        yield u

                # pagination: look for 'next' or 'older'
                next_link = (
                    soup.find("a", string=re.compile(r"next|older", re.I)) or
                    soup.find("a", rel="next")
                )
                if not next_link:
                    break
                page += 1

    def _looks_like_article(self, u: str) -> bool:
        sp = urlparse(u)
        if not self.same_domain(u): return False
        if re.search(r"/(tag|category|author|page|feed)/", sp.path, re.I): return False
        if sp.path.endswith((".xml",".jpg",".png",".pdf",".webp",".zip")): return False
        segs = [s for s in sp.path.strip("/").split("/") if s]
        return 1 <= len(segs) <= 3
    
    def extract_recipe(self, soup: BeautifulSoup, url: str, category: Optional[str] = None) -> RecipeDoc:
        doc = RecipeDoc.make(url)
        # JSON-LD first (many WPRM pages also embed it)
        j = self.extract_jsonld(soup)
        if j:
            for k, v in j.items():
                if not hasattr(doc, k):
                    continue
                # skip empty values from JSON-LD
                if v in (None, "", [], {}):
                    continue
                # never overwrite an already-set url/source
                if k in ("url", "source") and getattr(doc, k):
                    continue
                setattr(doc, k, v)
        # WPRM block
        w = soup.find("div", class_="wprm-recipe-container")
        if w:
            if not doc.title:
                t = w.find(class_="wprm-recipe-name")
                doc.title = clean(t.get_text()) if t else doc.title
            # image
            if not doc.image_url:
                img = w.find("img")
                doc.image_url = clean(img.get("src") or img.get("data-src")) if img else clean(get_meta_image(soup))
            # rating
            r = w.find(class_="wprm-recipe-rating-average")
            if r:
                try: doc.rating = float(r.get_text().strip())
                except: pass
            rc = w.find(class_="wprm-recipe-rating-count")
            if rc:
                try: doc.rating_count = int(rc.get_text().strip())
                except: pass
            # times
            def pick(c): 
                x = w.find(class_=c)
                return clean(x.get_text()) if x else None
            doc.prep_time = pick("wprm-recipe-prep_time") or doc.prep_time
            doc.cook_time = pick("wprm-recipe-cook_time") or doc.cook_time
            doc.total_time= pick("wprm-recipe-total_time") or doc.total_time
            # servings
            s = w.find(class_="wprm-recipe-servings")
            if s:
                txt = s.get_text().strip()
                doc.servings = int(txt) if txt.isdigit() else clean(txt)
            # calories
            cal = w.find(class_="wprm-recipe-calories")
            if cal:
                try: doc.calories = float(cal.get_text().strip())
                except: pass
            # course/cuisine
            cse = w.find(class_="wprm-recipe-course"); doc.course = clean(cse.get_text()) if cse else doc.course
            cui = w.find(class_="wprm-recipe-cuisine"); doc.cuisine = clean(cui.get_text()) if cui else doc.cuisine
            # ingredients
            ings = []
            ic = w.find(class_="wprm-recipe-ingredients-container")
            if ic:
                for ing in ic.find_all(class_="wprm-recipe-ingredient"):
                    parts=[]
                    for cls in ("wprm-recipe-ingredient-amount","wprm-recipe-ingredient-unit","wprm-recipe-ingredient-name","wprm-recipe-ingredient-notes"):
                        el = ing.find(class_=cls)
                        if el:
                            t = el.get_text().strip()
                            if t:
                                parts.append(t if "notes" not in cls else f"({t})")
                    txt = clean(" ".join(parts))
                    if txt: ings.append(txt)
            if ings: doc.ingredients = ings
            # instructions
            steps=[]
            ic2 = w.find(class_="wprm-recipe-instructions-container")
            if ic2:
                for ins in ic2.find_all(class_="wprm-recipe-instruction"):
                    t = ins.find(class_="wprm-recipe-instruction-text") or ins
                    txt = clean(t.get_text().strip())
                    if txt: steps.append(txt)
            if steps: doc.instructions = steps

        # Fallback title & image
        if not doc.title:
            h1 = soup.find("h1") or soup.find("title")
            doc.title = clean(h1.get_text()) if h1 else None
        if not doc.image_url:
            doc.image_url = clean(get_meta_image(soup))

        # Optional category (yours wants "Afro-tropical Recipes" etc. — grab from breadcrumbs or page tags if available)
        cat = soup.find("a", href=re.compile(r"/category/|/tag/"))
        doc.category = clean(cat.get_text()) if cat else doc.category

        return doc

