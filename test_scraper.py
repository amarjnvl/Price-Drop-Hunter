import logging
import requests
from bs4 import BeautifulSoup
from main import scrape_product_info, HEADERS

# Configure logging to show info
logging.basicConfig(level=logging.INFO, format="%(message)s")

TEST_URLS = [
    # Flipkart URLs (Failling for user)
    "https://www.flipkart.com/amaron-abr-pr-12apbtx50-5-ah-battery-bike/p/itm36cf7b38eabcd?pid=VEBHD9CMJ7A34CXA",
    "https://www.flipkart.com/honeytouch-atlas-folding-bed-single-mattress-no-assembly-required-metal-bed-183-cm-x-76-6-0-ft-2-49-ft/p/itm3ef05cbec8ac6?pid=BDDGENQUJFM6R5GZ",
    
    # Amazon URL (Working for user)
    "https://www.amazon.in/dp/B0G5G7LCJQ",
    
    # Myntra Example
    "https://www.myntra.com/tshirts/mr-bowerbird/mr-bowerbird-men-blue-solid-tailored-fit-round-neck-t-shirt/8890053/buy",
]

def test_scraping():
    print(f"{'='*20} STARTING SCRAPING TEST {'='*20}")
    for url in TEST_URLS:
        print(f"\nTesting: {url}")
        try:
            # We use the function from main.py directly
            # This function internally handles fetching and scraping
            info = scrape_product_info(url)
            
            if info:
                title = info.get("title")
                price = info.get("price")
                
                status_icon = "✅" if (title and price) else "❌"
                print(f"{status_icon} Result:")
                print(f"   Title: {title}")
                print(f"   Price: {price}")
            else:
                print("❌ Failed to scrape info (Returned None)")
                
        except Exception as e:
            print(f"❌ Exception occurred: {e}")

if __name__ == "__main__":
    test_scraping()
