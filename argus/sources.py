"""What Argus watches. (name, category, kind, url)

kind: rss | hf_daily | github_trending
category: ai | finance | india | ai_finance | research
Add or remove freely; a source that fails is logged and skipped, never fatal.
"""
from urllib.parse import quote_plus


def gnews(q: str, region: str = "US") -> str:
    lang = "en-IN" if region == "IN" else "en-US"
    return f"https://news.google.com/rss/search?q={quote_plus(q)}+when:1d&hl={lang}&gl={region}&ceid={region}:en"


SOURCES = [
    # ---------------- AI: labs and primary sources
    ("OpenAI", "ai", "rss", "https://openai.com/news/rss.xml"),
    ("Google AI", "ai", "rss", "https://blog.google/technology/ai/rss/"),
    ("Google DeepMind", "ai", "rss", "https://deepmind.google/blog/rss.xml"),
    ("Hugging Face blog", "ai", "rss", "https://huggingface.co/blog/feed.xml"),
    ("Anthropic (news)", "ai", "rss", gnews("Anthropic Claude")),
    ("Meta AI (news)", "ai", "rss", gnews('"Meta AI" OR Llama model')),
    ("xAI / Mistral / DeepSeek (news)", "ai", "rss", gnews("xAI OR Mistral OR DeepSeek OR Qwen model")),
    # ---------------- AI: press
    ("MIT Tech Review AI", "ai", "rss", "https://www.technologyreview.com/topic/artificial-intelligence/feed"),
    ("TechCrunch AI", "ai", "rss", "https://techcrunch.com/category/artificial-intelligence/feed/"),
    ("The Verge AI", "ai", "rss", "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml"),
    ("Ars Technica AI", "ai", "rss", "https://arstechnica.com/ai/feed/"),
    ("AI headlines", "ai", "rss", gnews("artificial intelligence breakthrough OR launch OR regulation")),
    # ---------------- AI: community signal
    ("Hacker News front page", "ai", "hn", "front"),      # official Algolia API; hnrss.org kept timing out
    ("Hacker News AI 80+", "ai", "hn", "ai"),
    # Reddit blocks anonymous RSS (403/429) as of 2026-11. Re-add via the Reddit API with an app token if wanted.
    ("GitHub trending", "ai", "github_trending", "https://github.com/trending?since=daily"),
    # ---------------- Research
    ("HF Daily Papers", "research", "hf_daily", "https://huggingface.co/api/daily_papers?limit=50"),
    ("arXiv cs.AI", "research", "rss", "https://rss.arxiv.org/rss/cs.AI"),
    ("arXiv cs.CL", "research", "rss", "https://rss.arxiv.org/rss/cs.CL"),
    ("arXiv q-fin", "research", "rss", "https://rss.arxiv.org/rss/q-fin"),
    # ---------------- Global finance
    ("Federal Reserve", "finance", "rss", "https://www.federalreserve.gov/feeds/press_all.xml"),
    ("ECB", "finance", "rss", "https://www.ecb.europa.eu/rss/press.html"),
    ("Bloomberg Markets", "finance", "rss", "https://feeds.bloomberg.com/markets/news.rss"),
    ("CNBC Markets", "finance", "rss", "https://www.cnbc.com/id/20910258/device/rss/rss.html"),
    ("CNBC Finance", "finance", "rss", "https://www.cnbc.com/id/10000664/device/rss/rss.html"),
    ("MarketWatch", "finance", "rss", "https://feeds.content.dowjones.io/public/rss/mw_topstories"),
    ("Markets headlines", "finance", "rss", gnews("stock market OR Wall Street OR Fed rates OR bond yields")),
    ("Big tech earnings", "finance", "rss", gnews("earnings Nvidia OR Microsoft OR Apple OR Alphabet OR Amazon OR Meta OR Tesla")),
    ("IPOs & mega deals", "finance", "rss", gnews("IPO OR acquisition billion")),
    ("Crypto", "finance", "rss", gnews("bitcoin OR ethereum OR crypto ETF")),
    ("Macro / oil / dollar", "finance", "rss", gnews("oil prices OR dollar index OR inflation data OR recession")),
    # ---------------- AI x finance
    ("AI funding & valuations", "ai_finance", "rss", gnews("AI startup raises OR valuation OR funding round billion")),
    ("AI chips & stocks", "ai_finance", "rss", gnews("AI chip stocks OR Nvidia OR TSMC OR AMD AI")),
    ("AI in finance", "ai_finance", "rss", gnews("AI banking OR AI trading OR AI fintech")),
    # ---------------- India
    ("RBI", "india", "rss", "https://www.rbi.org.in/pressreleases_rss.xml"),
    ("SEBI", "india", "rss", "https://www.sebi.gov.in/sebirss.xml"),
    ("ET Markets", "india", "rss", "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms"),
    ("Mint Markets", "india", "rss", "https://www.livemint.com/rss/markets"),
    ("India markets headlines", "india", "rss", gnews("Sensex OR Nifty OR Indian startup funding", "IN")),
]
