import json

def generate_images():
    results = []
    for level in range(1, 2001):
        # Determine tags based on level to simulate "easy" to "impossible"
        if level <= 400:
            tags = "nature,landscape,clear"
        elif level <= 800:
            tags = "nature,forest,wildlife"
        elif level <= 1200:
            tags = "nature,macro,detail"
        elif level <= 1600:
            tags = "nature,pattern,texture"
        else:
            tags = "nature,abstract,micro"

        # 2000x2000 for high resolution square
        link = f"https://loremflickr.com/2000/2000/{tags}?lock={level}"
        results.append({"level": level, "link": link})

    with open("images.json", "w") as f:
        json.dump(results, f, indent=2)
    print(f"Successfully saved {len(results)} image links to images.json")

if __name__ == "__main__":
    generate_images()
