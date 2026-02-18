from curl_cffi import requests

url = "https://www.flipkart.com/amaron-abr-pr-12apbtx50-5-ah-battery-bike/p/itm36cf7b38eabcd?pid=VEBHD9CMJ7A34CXA"

try:
    print(f"Fetching {url} using curl_cffi...")
    session = requests.Session()
    
    # We impersonate a real browser (Chrome 110)
    resp = session.get(url, impersonate="chrome110", timeout=15)
    
    print(f"Status Code: {resp.status_code}")
    print(f"Content Length: {len(resp.text)}")
    
    with open("flipkart_debug.html", "w", encoding="utf-8") as f:
        f.write(resp.text)
        
except Exception as e:
    print(f"Error: {e}")
        
except Exception as e:
    print(f"Error: {e}")
