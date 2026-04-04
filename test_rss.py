import urllib.request
import xml.etree.ElementTree as ET

headers = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/122.0',
    'Accept': 'text/xml'
}
feeds = [
    'https://www.moneycontrol.com/rss/MCtopnews.xml',
    'https://www.moneycontrol.com/rss/business.xml',
    'https://www.moneycontrol.com/rss/marketnews.xml',
    'https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms',
    'https://economictimes.indiatimes.com/markets/stocks/rssfeeds/2146842.cms',
    'https://www.businesstoday.in/rss/markets',
    'https://feeds.bloomberg.com/markets/news.rss',
    'https://feeds.bloomberg.com/economics/news.rss',
    'https://www.livemint.com/rss/markets'
]

for f in feeds:
    try:
        req = urllib.request.Request(f, headers=headers)
        res = urllib.request.urlopen(req, timeout=5).read()
        root = ET.fromstring(res)
        item = root.find('.//item')
        if item is not None:
            pub = item.find('pubDate')
            print(f.split('/')[-1].ljust(20) + ' | ' + (pub.text if pub is not None else 'No pubDate'))
    except Exception as e:
        print(f.split('/')[-1].ljust(20) + ' | ERROR: ' + str(e)[:40])
