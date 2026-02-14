# pip install pymongo bs4 lxml
import os, re, json, time, logging, io
from dataclasses import dataclass, asdict, field
from typing import Optional, List, Dict, Any, Iterable, Tuple
from urllib.parse import urlparse, urljoin
from datetime import datetime

import requests
from bs4 import BeautifulSoup, Tag, NavigableString
from embedder.base_encoder import BaseEncoder
from embedder.hugging_face_embedder import HFEmbedder
from pymongo import MongoClient, errors
from pymongo.operations import UpdateOne

# ---------------------------
# 0) Utilities & Schema
# ---------------------------

def clean(s: Optional[str]) -> Optional[str]:
    if not s: return None
    s = re.sub(r"\s+", " ", s).strip()
    s = re.sub(r"\bclick here\b.*", "", s, flags=re.I)
    return s or None

def get_meta_image(soup: BeautifulSoup) -> Optional[str]:
    for sel in [
        "meta[property='og:image']",
        "meta[name='og:image']",
        "meta[name='twitter:image']",
        "meta[property='twitter:image']"
    ]:
        m = soup.select_one(sel)
        if m and m.get("content"):
            return m["content"]
    img = soup.find("img")
    return (img.get("src") or img.get("data-src")) if img else None

@dataclass
class RecipeDoc:
    title: Optional[str] = None
    url: Optional[str] = None
    source: Optional[str] = None
    category: Optional[str] = None
    ingredients: List[str] = field(default_factory=list)
    instructions: List[str] = field(default_factory=list)
    prep_time: Optional[str] = None
    cook_time: Optional[str] = None
    total_time: Optional[str] = None
    servings: Optional[Any] = None
    calories: Optional[float] = None
    rating: Optional[float] = None
    rating_count: Optional[int] = None
    course: Optional[str] = None
    cuisine: Optional[str] = None
    notes: Optional[str] = None
    image_url: Optional[str] = None
    needs_review: Optional[bool] = None
    section: List[Dict[str, Any]] = field(default_factory=list)  # keep your "section": []
    # internal
    scraped_at: Optional[datetime] = None

    @staticmethod
    def make(url: str) -> "RecipeDoc":
        host = urlparse(url).netloc
        RecipeDocs = RecipeDoc(url=url, source=host, scraped_at=datetime.utcnow())
        return RecipeDocs

    def finalize(self):
        # mark needs_review conservatively
        self.needs_review = bool((len(self.ingredients) < 2) or (len(self.instructions) < 2))
        # normalize empties to None when appropriate
        for k, v in list(asdict(self).items()):
            if isinstance(v, list) and len(v) == 0:
                setattr(self, k, [] if k in ("ingredients","instructions","section") else None)
            elif v == "":
                setattr(self, k, None)

# ---------------------------
# 1) Reusable Soup/HTTP client
# ---------------------------

class SoupClient:
    def __init__(self, base_domain: str, user_agent: str = None, embedder= None):
        self.base_domain = base_domain
        self.base_url = f"https://{base_domain}"
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": user_agent or
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
        })
        self.log = logging.getLogger(self.__class__.__name__)

    def same_domain(self, url: str) -> bool:
        host = urlparse(url).netloc.lower()
        return host == self.base_domain.lower() or host == self.base_domain.lower().replace("www.", "")

    def fetch_soup(self, url: str, timeout: int = 30) -> BeautifulSoup:
        r = self.session.get(url, timeout=timeout)
        r.raise_for_status()
        return BeautifulSoup(r.content, "lxml")

    def close(self):
        self.session.close()

# ---------------------------
# 2) Reusable persistence
# ---------------------------
class JsonArraySink:
    """
    Append-safe JSON array writer.
    - Creates file with `[` ... `]`
    - If file exists, removes trailing `]`, appends items, and re-closes.
    """
    def __init__(self, path: str):
        self.path = path
        self._opened = False
        self._first = True
        self.f = None

    def _prepare(self):
        if self._opened:
            return

        # Ensure file exists with an empty array
        if not os.path.exists(self.path):
            with open(self.path, "w", encoding="utf-8") as f:
                f.write("[\n]")

        self.f = open(self.path, "r+", encoding="utf-8")

        # Find the position of the final ']' from the end
        self.f.seek(0, io.SEEK_END)
        end = self.f.tell()
        step = min(4096, end)
        pos = end
        last_bracket = -1
        while pos > 0:
            pos = max(0, pos - step)
            self.f.seek(pos)
            chunk = self.f.read(step)
            j = chunk.rfind("]")
            if j != -1:
                last_bracket = pos + j
                break

        if last_bracket == -1:
            # Corrupt file: reset to empty array
            self.f.seek(0); self.f.truncate(0); self.f.write("[\n]"); self.f.flush()
            last_bracket = 2  # index of ']' in "[\n]"

        # Decide "is first item?" by inspecting the content BEFORE the ']'
        self.f.seek(0)
        prefix = self.f.read(last_bracket).strip()   # content up to (but not including) ']'
        # Empty array has only '[' (possibly with whitespace/newline)
        self._first = (prefix == "[")

        # Now remove the closing ']' so we can append
        self.f.seek(last_bracket)
        self.f.truncate()

        self._opened = True

    def write_many(self, docs: List[Dict[str, Any]]):
        if not docs:
            return
        self._prepare()

        for d in docs:
            if not self._first:
                self.f.write(",\n")
            else:
                # First item: no leading comma
                self._first = False
            self.f.write(json.dumps(d, ensure_ascii=False, indent=2, default=str))

        # Restore the closing bracket
        self.f.write("\n]")
        self.f.flush()

    def close(self):
        if self.f:
            self.f.close()
        self._opened = False

class MongoSink:
    def __init__(self, uri: str, db: str = "recipes_db", coll: str = "recipes"):
        self.client = MongoClient(uri, retryWrites=True, serverSelectionTimeoutMS=10000)
        self.col = self.client[db][coll]
        self._ensure_indexes()

    def _ensure_indexes(self):
        self.col.create_index("url", unique=True)
        self.col.create_index("title")
        self.col.create_index("category")
        self.col.create_index("scraped_at")

    def upsert_batch(self, docs: List[Dict[str, Any]]):
        if not docs: return
        ops = []
        now = datetime.utcnow()
        for d in docs:
            d = d.copy()
            d.setdefault("scraped_at", now)
            ops.append(UpdateOne({"url": d["url"]}, {"$set": d, "$setOnInsert": {"created_at": now}}, upsert=True))
        try:
            self.col.bulk_write(ops, ordered=False)
        except errors.BulkWriteError as e:
            # duplicates or minor issues won't halt unordered bulk
            pass

    def close(self):
        self.client.close()

class DualSink:
    def __init__(self, json_sink: Optional[JsonArraySink], mongo_sink: Optional[MongoSink]):
        self.json = json_sink
        self.mongo = mongo_sink
    def write_many(self, docs: List[Dict[str, Any]]):
        if self.json: self.json.write_many(docs)
        if self.mongo: self.mongo.upsert_batch(docs)
    def close(self):
        if self.json: self.json.close()
        if self.mongo: self.mongo.close()

# ---------------------------
# 3) Base scraper (shared BS + JSON-LD)
# ---------------------------

class BaseRecipeScraper(SoupClient):
    HEADING_TAGS = ("h1","h2","h3","h4","h5","h6")

    def __init__(
        self,
        *args,
        embedder: "BaseEncoder | None" = None,
        embedding_fields= None,
        **kwargs
    ):
        """
        embedder: HFEmbedder(), optional
        embedding_fields: list of (source_field, target_field) like:
            [("title", "title_emb"), ("instructions_text", "instr_emb")]
        """
        super().__init__(*args, **kwargs)
        self.embedder = embedder
        self.embedding_fields = embedding_fields or []

    def extract_jsonld(self, soup: BeautifulSoup) -> Optional[Dict[str, Any]]:
        def to_list(x): return x if isinstance(x, list) else [x]
        for tag in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(tag.string or "{}")
            except Exception:
                continue
            nodes = (data.get("@graph", [data]) if isinstance(data, dict)
                     else (data if isinstance(data, list) else []))
            for n in nodes:
                if not isinstance(n, dict): continue
                t = n.get("@type")
                if t == "Recipe" or (isinstance(t, list) and "Recipe" in t):
                    doc = RecipeDoc()
                    doc.title = clean(n.get("name"))
                    # ingredients
                    ings = []
                    for ing in to_list(n.get("recipeIngredient") or []):
                        if isinstance(ing, dict):
                            ings.append(clean(ing.get("name") or ing.get("text")))
                        else:
                            ings.append(clean(str(ing)))
                    doc.ingredients = [x for x in ings if x]
                    # instructions
                    steps = []
                    for st in to_list(n.get("recipeInstructions") or []):
                        if isinstance(st, dict):
                            steps.append(clean(st.get("text") or st.get("name")))
                        else:
                            steps.append(clean(str(st)))
                    doc.instructions = [x for x in steps if x]
                    doc.prep_time = clean(n.get("prepTime"))
                    doc.cook_time = clean(n.get("cookTime"))
                    doc.total_time = clean(n.get("totalTime"))
                    doc.servings = n.get("recipeYield")
                    doc.image_url = clean((n.get("image") or {}).get("url") if isinstance(n.get("image"), dict) else (n.get("image")[0] if isinstance(n.get("image"), list) else n.get("image")))
                    doc.course = clean(n.get("recipeCategory")) if isinstance(n.get("recipeCategory"), str) else None
                    doc.cuisine = clean(n.get("recipeCuisine")) if isinstance(n.get("recipeCuisine"), str) else None
                    return asdict(doc)
        return None

    # site-specific scrapers override these two:
    def discover_urls(self) -> Iterable[str]:
        raise NotImplementedError
    def extract_recipe(self, soup: BeautifulSoup, url: str, category: Optional[str] = None) -> RecipeDoc:
        raise NotImplementedError

    # shared streaming loop
    def stream(self, sink: DualSink, delay: float = 0.3, limit: Optional[int] = None,
               batch_size: int = 50, resume_file: Optional[str] = None) -> int:
        processed = set()
        if resume_file and os.path.exists(resume_file):
            processed = {l.strip() for l in open(resume_file, "r", encoding="utf-8")}
            self.log.info(f"[resume] {len(processed)} URLs already done")

        batch, saved = [], 0
        try:
            for i, url in enumerate(self.discover_urls(), 1):
                if limit and i > limit: break
                if not self.same_domain(url): continue
                if url in processed: continue

                try:
                    soup = self.fetch_soup(url)
                    doc = self.extract_recipe(soup, url)
                    doc.finalize()
                    batch.append(asdict(doc))
                except Exception as e:
                    self.log.warning(f"[skip] {url} -> {e}")

                # persist progress
                if resume_file:
                    with open(resume_file, "a", encoding="utf-8") as rf:
                        rf.write(url + "\n")

                if len(batch) >= batch_size:
                    self._apply_embeddings(batch)
                    sink.write_many(batch); saved += len(batch); batch = []
                if i % 25 == 0:
                    self.log.info(f"…processed {i}, saved {saved}")
                time.sleep(delay)

            if batch:
                self._apply_embeddings(batch)
                sink.write_many(batch); saved += len(batch)
        finally:
            sink.close()
        self.log.info(f"[done] saved {saved}")
        return saved

    def _apply_embeddings(self, batch: List[Dict[str, Any]]) -> None:
        """
        Applies embeddings to specified fields in a batch of documents.

        For each (source_field, destination_field) pair in `self.embedding_fields`, this method:
        - Extracts the value from `source_field` in each document of the batch.
        - Converts the value to a string. If the value is a list, joins its elements with newlines.
        - Handles `None` values by converting them to empty strings.
        - Uses `self.embedder.encode` to generate embeddings for the processed texts.
        - Stores the resulting embedding vector in `destination_field` of each document.

        If `self.embedder`, `self.embedding_fields`, or `batch` is not set or empty, the method returns immediately.

        Args:
            batch (List[Dict[str, Any]]): A list of documents to process, where each document is a dictionary.

        Returns:
            None
        """
        if not self.embedder or not self.embedding_fields or not batch:
            return

        for src_field, dst_field in self.embedding_fields:
            # Gather texts; coerce to strings.
            texts: List[str] = []
            for doc in batch:
                val = doc.get(src_field)
                if isinstance(val, list):
                    # sensible default for ingredients/instructions: join with newline
                    joined = "\n".join(str(x) for x in val)
                    texts.append(joined)
                elif val is None:
                    texts.append("")
                else:
                    texts.append(str(val))

            embs = self.embedder.encode(texts)
            # Write back
            for document, vec in zip(batch, embs):
                document[dst_field] = vec


