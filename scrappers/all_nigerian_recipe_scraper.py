import re
from typing import Iterable, List, Optional
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup, NavigableString, Tag
from scrappers.base_scrapper import BaseRecipeScraper, RecipeDoc, clean, get_meta_image


class AllNigerianRecipesScraper(BaseRecipeScraper):
    # two-level paths like /beans/beans-porridge/
    ALLOWED_CATS = {
        "beans","snacks","soups","stews","salad","breakfast","rice",
        "yam","plantain","swallow","drinks","desserts","meat","fish", "chicken"
    }
    PAT_ING   = re.compile(r"\bingredient|what you need\b", re.I)
    PAT_NOTEI = re.compile(r"\bnotes?\b.*ingredient", re.I)
    PAT_BEFORE= re.compile(r"\bbefore you cook\b", re.I)
    PAT_INSTR = re.compile(r"\b(preparation|directions|method|instructions|cooking)\b", re.I)
# Map first path segment (category) -> course
    COURSE_BY_CATEGORY = {
        "soups": "Soup",
        "stews": "Stew",
        "snacks": "Snack",
        "drinks": "Drink",
        "desserts": "Dessert",
        "breakfast": "Breakfast",
        "salad": "Salad",
        "rice": "Main Course",
        "beans": "Main Course",
        "yam": "Main Course",
        "plantain": "Main Course",
        "meat": "Main Course",
        "fish": "Main Course",
        "chicken": "Main Course",   # <-- add chicken
        "swallow": "Side Dish",
    }
    # Fallback keyword hints from URL/title if category isn’t enough
    COURSE_BY_KEYWORD = [
        (r"\bsoup(s)?\b", "Soup"),
        (r"\bstew(s)?\b", "Stew"),
        (r"\bsalad(s)?\b", "Salad"),
        (r"\b(snack|small[-\s]?chops)\b", "Snack"),
        (r"\b(drink|juice|smoothie)\b", "Drink"),
        (r"\bbreakfast\b", "Breakfast"),
        (r"\bdessert(s)?\b", "Dessert"),
    ]

    def __init__(self, base_domain="www.allnigerianrecipes.com", index_url="https://www.allnigerianrecipes.com/other/sitemap/"):
        super().__init__(base_domain)
        self.index_url = index_url

    def _is_two_level_recipe(self, url: str) -> bool:
        sp = urlparse(url); segs = [s for s in sp.path.strip("/").split("/") if s]
        if len(segs) != 2: return False
        if segs[0].lower() not in self.ALLOWED_CATS: return False
        bad = {"page","tag","tags","category","categories","author","search"}
        if any(s in bad for s in segs): return False
        if sp.path.lower().endswith((".xml",".pdf",".zip",".jpg",".jpeg",".png",".webp",".mp4",".mov")):
            return False
        return True

    def discover_urls(self) -> Iterable[str]:
        soup = self.fetch_soup(self.index_url)
        seen = set()
        for a in soup.select("a[href]"):
            u = urljoin(self.index_url, a["href"])
            if self.same_domain(u) and self._is_two_level_recipe(u) and u not in seen:
                seen.add(u); yield u

    def _li_text_only(self, li: Tag) -> str:
        parts=[]
        for ch in li.contents:
            if isinstance(ch, NavigableString): parts.append(str(ch))
            elif isinstance(ch, Tag) and ch.name not in ("ul","ol","iframe"):
                parts.append(ch.get_text(" ", strip=True))
        return clean(" ".join(parts)) or ""

    def _collect_after(self, h: Tag) -> List[str]:
        lvl = int(h.name[1])
        out=[]
        for sib in h.next_siblings:
            if isinstance(sib, Tag) and sib.name in self.HEADING_TAGS and int(sib.name[1])<=lvl: break
            if isinstance(sib, Tag):
                if sib.name in ("ul","ol"):
                    lis = sib.find_all("li", recursive=False) or sib.find_all("li")
                    for li in lis:
                        t = self._li_text_only(li)
                        if t: out.append(t)
                elif sib.name in ("p","div","blockquote"):
                    t = clean(sib.get_text(" ", strip=True))
                    if t: out.append(t)
            elif isinstance(sib, NavigableString):
                t = clean(str(sib)); 
                if t: out.append(t)
        return [x for x in out if not x.lower().startswith("video of ")]

    def extract_recipe(self, soup: BeautifulSoup, url: str, category: Optional[str] = None) -> RecipeDoc:
        doc = RecipeDoc.make(url)
        # JSON-LD first
        j = self.extract_jsonld(soup)
        if j:
            for k,v in j.items():
                if hasattr(doc, k): setattr(doc, k, v)
        # Title fallback
        if not doc.title:
            title = soup.find("h1") or soup.find("title")
            doc.title = clean(title.get_text()) if title else None

        root = soup.select_one(".entry-content") or soup
        sections_ing, sections_instr, notes = [], [], []

        for h in root.find_all(self.HEADING_TAGS):
            ht = h.get_text(" ", strip=True) or ""
            if self.PAT_NOTEI.search(ht):
                it = self._collect_after(h); 
                if it: notes.append({"title": ht, "items": it})
            elif self.PAT_ING.search(ht):
                it = [x for x in self._collect_after(h) if self._looks_like_ingredient(x)]
                if it: sections_ing.append({"title": ht, "items": it})
            elif self.PAT_BEFORE.search(ht) or self.PAT_INSTR.search(ht):
                st = self._collect_after(h); 
                if st: sections_instr.append({"title": ht, "steps": st})

        # Flatten
        for s in sections_ing + notes:
            doc.ingredients.extend(s["items"])
        for s in sections_instr:
            doc.instructions.extend(s["steps"])

        # Notes & image
        if notes:
            # concatenate notes into a single field
            doc.notes = " ".join(["; ".join(n.get("items", [])) for n in notes if n.get("items")])
        if not doc.image_url:
            doc.image_url = clean(get_meta_image(soup))

        # keep your "section" array (optional: include headings)
        for s in sections_instr:
            for i, step in enumerate(s["steps"], 1):
                doc.section.append({"type":"instruction","title":s["title"],"order":i,"text":step})
        for s in sections_ing:
            for i, item in enumerate(s["items"], 1):
                doc.section.append({"type":"ingredient","title":s["title"],"order":i,"text":item})

        # Infer category (first path segment) and map to a course
        if not getattr(doc, "category", None) or not getattr(doc, "course", None):
            cat, crs = self._infer_category_and_course(url, doc.title)
            if not doc.category:
                doc.category = cat
            if not doc.course:
                doc.course = crs

        return doc

    def _looks_like_ingredient(self, text: str) -> bool:
        if len(text.split()) > 15: return False
        return bool(re.search(r"\b(\d+|cup|cups|tsp|tbsp|teaspoon|tablespoon|g|kg|ml|l|gram|salt|pepper|onion|oil|water)\b", text, re.I))

    @staticmethod
    def _slug_to_title(slug: Optional[str]) -> Optional[str]:
        return slug.replace("-", " ").title() if slug else None

    def _infer_category_and_course(self, url: str, title: Optional[str]) -> tuple[Optional[str], Optional[str]]:
        sp = urlparse(url)
        segs = [s for s in sp.path.strip("/").split("/") if s]
        cat_slug = segs[0] if segs else None
        category = self._slug_to_title(cat_slug) if cat_slug else None

        course = self.COURSE_BY_CATEGORY.get(cat_slug)
        if not course:
            hay = f"{url} {(title or '')}".lower()
            for pat, crs in self.COURSE_BY_KEYWORD:
                if re.search(pat, hay):
                    course = crs
                    break
        if not course and category:
            course = "Main Course"
        return category, course