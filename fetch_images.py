import requests
import json
import time
from concurrent.futures import ThreadPoolExecutor
import os

def fetch_static_url(level):
    idx = (level - 1) // 400
    tags = ["nature,landscape", "nature,forest", "nature,macro", "nature,pattern", "nature,abstract"]
    tag = tags[min(idx, 4)]

    # We try different locks to find a unique, non-default image from LoremFlickr
    # or fallback to Picsum if we can't find one.

    # Try LoremFlickr first (preferred for nature tags)
    for offset in [0, 10000, 20000, 30000, 40000]:
        url = f"https://loremflickr.com/2000/2000/{tag}?lock={level + offset}"
        try:
            r = requests.get(url, timeout=10)
            if "cache/resized" in r.url and "defaultImage" not in r.url:
                return {"level": level, "link": r.url}
        except:
            pass
        time.sleep(0.1)

    # Fallback to Picsum for a guaranteed static link
    # Picsum IDs are unique. We use level as ID if it's within range, or offset it.
    for pid in [level, level + 1000, level + 2000]:
        try:
            r = requests.get(f"https://picsum.photos/id/{pid % 1000}/2000/2000", allow_redirects=False, timeout=5)
            if r.status_code == 302:
                loc = r.headers.get('Location')
                if loc: return {"level": level, "link": loc}
        except:
            pass

    # Final fallback if all else fails
    return {"level": level, "link": f"https://loremflickr.com/2000/2000/{tag}?lock={level}"}

def main():
    print("Starting resolution of 2000 unique, static nature images...")
    results = []

    # Process in batches with thread pool
    with ThreadPoolExecutor(max_workers=20) as executor:
        for i in range(1, 2001, 100):
            print(f"Resolving levels {i} to {min(i+99, 2000)}...")
            batch_levels = range(i, min(i+100, 2001))
            batch_results = list(executor.map(fetch_static_url, batch_levels))
            results.extend(batch_results)

            # Intermediate save
            with open("images.json", "w") as f:
                json.dump(results, f, indent=2)

    # Deduplicate and ensure completeness
    links_seen = set()
    final_data = []
    for d in results:
        if d['link'] in links_seen:
            # If duplicate, try one more time with a high random lock
            res = fetch_static_url(d['level'] + 55555)
            res['level'] = d['level']
            d = res
        seen_links.add(d['link'])
        final_data.append(d)

    final_data.sort(key=lambda x: x["level"])
    with open("images.json", "w") as f:
        json.dump(final_data, f, indent=2)

    resolved = sum(1 for d in final_data if "cache/resized" in d['link'] or "picsum.photos" in d['link'])
    print(f"Final resolution rate: {resolved}/2000")

if __name__ == "__main__":
    main()
