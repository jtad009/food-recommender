
import logging
import os
from scrappers.all_nigerian_recipe_scraper import AllNigerianRecipesScraper
from scrappers.base_scrapper import DualSink, JsonArraySink, MongoSink
from scrappers.yummy_medley_scraper import YummyMedleyScraper


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    # Choose sinks (one or both)
    json_sink = JsonArraySink("recipes_unified.json")          # append-safe JSON array
    mongo_sink = MongoSink(os.getenv("MONGODB_URI",""), db="nigeria_recipes", coll="recipes") if os.getenv("MONGODB_URI") else None
    sink = DualSink(json_sink, mongo_sink)

    # Pick a scraper
    # s = AllNigerianRecipesScraper()
    s = YummyMedleyScraper()

    # Stream with batch writes and resume
    s.stream(
        sink=sink,
        delay=0.3,
        limit=500,                 # or None
        batch_size=50,
        resume_file="recipes.resume"
    )
