import os

# ---------------------------------------------------------------------------
# 🤖 제미나이 모델명 빈칸 자동 방어 로직
#    ✅ 2순위 반영: gemini-1.5-* 는 전부 셧다운(404). 별칭 'gemini-flash-latest'
#       사용 → 구글이 최신 flash(현재 3.5)로 자동 라우팅, 재하드코딩 불필요.
# ---------------------------------------------------------------------------
# 이미 종료된 모델(1.0/1.5/2.0 계열)은 env에 들어와 있어도 방어 → 살아있는 기본값으로 교체.
# 2.5-flash 는 2026-10-16 종료 예정이므로 그 전 교체 필요(대안: gemini-flash-latest / 3.5).
_DEAD_MODEL_PREFIXES = ("gemini-1.0", "gemini-1.5", "gemini-2.0")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "").strip()
if not GEMINI_MODEL or GEMINI_MODEL.startswith(_DEAD_MODEL_PREFIXES):
    GEMINI_MODEL = "gemini-flash-latest"
os.environ["GEMINI_MODEL"] = GEMINI_MODEL

# ---------------------------------------------------------------------------
# 1. 관심 키워드 (🚀 최신 AI 모델명, VC 펀딩 단계, 매크로 지표 완벽 확장!)
# ---------------------------------------------------------------------------
IMPACT_KW = [
    "impact investing", "climate tech", "carbon neutral", "net zero",
    "energy transition", "renewable energy", "esg", "sustainability",
    "green fund", "climate fund", "cleantech", "decarbonization", "ev infrastructure",
    "임팩트투자", "기후테크", "탄소중립", "넷제로", "에너지전환",
    "재생에너지", "지속가능", "녹색기금", "기후펀드", "사회적기업",
    "소셜벤처", "그린뉴딜", "클린테크", "순환경제", "이차전지"
]

AI_KW = [
    "artificial intelligence", "generative ai", "llm", "large language model",
    "gpt", "gpt-4o", "gpt-5", "o3", "o4-mini", "claude", "claude sonnet", "claude opus",
    "gemini", "gemini 2.0", "gemini 2.5", "openai", "anthropic", "deepmind",
    "grok", "deepseek", "mistral", "qwen", "kimi", "cursor", "windsurf", "codex",
    "ai startup", "ai investment", "ai fund", "ai chip", "gpu", "semiconductor",
    "ai agent", "autonomous agent", "copilot", "multimodal", "data center",
    "인공지능", "생성형", "거대언어모델", "에이전트", "ai 반도체", "엔비디아", "데이터센터"
]

ALT_KW = [
    "private equity", "venture capital", "private debt", "private credit",
    "infrastructure fund", "real estate fund", "secondary fund", "buyout",
    "growth equity", "fund of funds", "lp", "gp", "dry powder",
    "series a", "series b", "series c", "series d", "series e",
    "seed round", "pre-seed", "growth round", "bridge round", "late stage",
    "fundraising", "vc funding", "unicorn", "ipo", "pre-ipo", "valuation",
    "exit", "take private", "continuation fund", "buy and build",
    "대체투자", "사모펀드", "벤처캐피탈", "사모채권", "인프라펀드",
    "부동산펀드", "세컨더리", "바이아웃", "그로스에쿼티", "출자자",
    "운용사", "드라이파우더", "블라인드펀드", "모태펀드", "공제회",
    "스타트업", "투자유치", "펀딩", "시리즈a", "시리즈b", "프리ipo", "인수", "합병", "유니콘"
]

MACRO_KW = [
    "fomc", "federal reserve", "interest rate", "inflation", "cpi",
    "gdp", "recession", "soft landing", "geopolitics", "tariff",
    "trade war", "supply chain", "sanctions", "oil price", "opec",
    "treasury yield", "10-year treasury", "yield curve", "dot plot",
    "기준금리", "인플레이션", "물가상승", "경기침체", "연준", "한국은행",
    "지정학", "관세", "무역전쟁", "공급망", "제재", "유가", "환율",
    "미중갈등", "중동", "우크라이나", "대만", "통화정책", "금리", "스콧베센트", "트럼", "거시경제"
]

INSIGHTS_KW = [
    "mckinsey", "맥킨지", "bcg", "bain", "베인", "deloitte", "딜로이트",
    "pwc", "ey", "kpmg", "sloan", "harvard business", "hbr", "insights",
    "strategy+business", "executive", "ceo survey", "megatrend", "메가트렌드",
    "whitepaper", "outlook", "survey", "report", "strategic framework",
    "보고서", "전망", "조사", "컨설팅", "트렌드", "인사이트", "산업동향"
]

INTEREST_KEYWORDS = sorted(set(IMPACT_KW + AI_KW + ALT_KW + MACRO_KW + INSIGHTS_KW))

# ✅ 2순위 반영: hackernews.py 는 keywords[:4] 만 사용하므로 INTEREST_KEYWORDS(정렬)를
#    그대로 쓰면 알파벳 앞 4개(무의미)만 검색됨 → HN용 소수 핵심어를 따로 둔다.
HN_KEYWORDS = [
    "AI", "venture capital", "climate tech", "startup",
    "OpenAI", "LLM", "energy transition", "semiconductor",
]

# ---------------------------------------------------------------------------
# 2. 카테고리 및 발송/점수 설정
# ---------------------------------------------------------------------------
CATEGORIES = {
    "🌱 임팩트": IMPACT_KW,
    "🤖 AI": AI_KW,
    "💼 대체투자": ALT_KW,
    "🌐 거시·정책·지정학": MACRO_KW,
    "👔 MBB·Big4 인사이트": INSIGHTS_KW,
}

MAX_PER_CATEGORY_DICT = {
    "🌱 임팩트": 5,
    "🤖 AI": 5,
    "💼 대체투자": 5,
    "🌐 거시·정책·지정학": 5,
    "👔 MBB·Big4 인사이트": 5,
}
MAX_PER_CATEGORY = 5

OVERSEAS_PREFERRED_DOMAINS = ["🌱 임팩트", "🤖 AI", "💼 대체투자", "👔 MBB·Big4 인사이트"]
REGION_WEIGHT = {"global": 1.35, "korea": 1.0}
LLM_SEND_MIN_SCORE = 0

# ── 운영 노브(팀 운영자가 조정하는 값) ──
SLACK_MAX_LENGTH = 3900     # 슬랙 자동분할(~4000자) 방지 상한
SLACK_HEADER = ""           # 예: "📰 *ISQ Daily News | {date}*"  (빈 문자열이면 헤더 없음)
MIN_CATEGORY_NEWS = 3       # 카테고리 최소 노출 목표(미달 시 규칙랭킹으로 보충)
TRANSLATE_TITLES = True     # 발송 기사 제목 한글 번역(영문만, 실패 시 원문 유지)

SIMILARITY_THRESHOLD = 0.72
WATCHLIST_WEIGHT = 2.5       # 관심기업 존재감 극대화
SOFT_PENALTY_KEYWORDS = [
    "특징주", "목표가", "상한가", "하한가", "종목추천", "리딩", "주가전망"
]

# ---------------------------------------------------------------------------
# 3. 블랙리스트 (지자체·소상공인·가십 철저 차단)
# ---------------------------------------------------------------------------
# ✅ 비-뉴스(채용공고·행사·수상·부고 등) 하드 차단 — is_relevant 에서 사용
NON_NEWS_KEYWORDS = [
    # 채용
    "we're hiring", "we are hiring", "now hiring", "job opening", "job opportunity",
    "apply now", "join our team", "career opportunity", "director of", "head of",
    "vp of", "chief of", "is hiring", "vacancy", "recruit",
    "채용", "공고", "모집", "구인", "리크루팅", "인재 영입",
    # 행사/세미나/시상
    "webinar", "join us", "register now", "rsvp", "save the date", "conference invite",
    "세미나", "웨비나", "포럼 개최", "행사 안내", "참가 신청", "참가신청",
    "컨퍼런스", "시상", "수상자 발표", "공모전", "설명회",
    # 부고/인사
    "obituary", "부고", "인사발령", "동정",
]

BLACKLIST_KEYWORDS = [
    "coupon", "promo code", "discount code", "% off", "best deals",
    "best price", "buy now", "airdoctor", "booking.com", "best laptop",
    "laptop review", "celebrity", "sports", "entertainment", "gaming",
    "movie", "tv show", "gossip", "github repo", "code walkthrough",
    "배임", "횡령", "파업",
    "중기자금", "소상공인", "지역화폐", "테크노파크", "지자체",
    "인천시", "서울시", "경기도", "부산시", "대구시", "광주시", "대전시", "울산시",
    "경남도", "경북도", "전남도", "전북도", "충남도", "충북도", "강원도", "제주도",
    "특례보증", "육성자금", "이차보전", "도청", "시청"
]

# ---------------------------------------------------------------------------
# 4. 중앙 통제식 RSS 피드 메타데이터 (Tier 및 Priority 완벽 적용)
# - Primary (우선순위 5) : 무조건 최우선 검토되는 A급 핵심 출처
# - Supplemental (우선순위 3~4) : 보조 출처 (LLM 후보군으로 주로 활용)
#   ✅ 3순위 반영: BCG Insights 제거 (https://www.bcg.com/rss 는 유효 피드가
#      아니라 매 실행 404. BCG 콘텐츠는 아래 'MBB/Big4 인사이트' Google News
#      쿼리가 커버함)
# ---------------------------------------------------------------------------
RSS_SOURCE_METADATA = {
    # 🌱 임팩트 (가장 중요 -> A급 매체 최대 포진)
    "Impact Alpha": {"url": "https://impactalpha.com/feed/", "category": "🌱 임팩트", "tier": "primary", "priority": 5},
    "NextBillion": {"url": "https://nextbillion.net/feed/", "category": "🌱 임팩트", "tier": "primary", "priority": 5},
    "SSIR": {"url": "https://ssir.org/site/rss_2.0/", "category": "🌱 임팩트", "tier": "primary", "priority": 5},
    "Pioneers Post": {"url": "https://www.pioneerspost.com/rss.xml", "category": "🌱 임팩트", "tier": "primary", "priority": 5},
    "Carbon Brief": {"url": "https://www.carbonbrief.org/feed/", "category": "🌱 임팩트", "tier": "primary", "priority": 5},
    "Responsible Investor": {"url": "https://www.responsible-investor.com/feed/", "category": "🌱 임팩트", "tier": "primary", "priority": 5},
    "ImpactOn (임팩트온)": {"url": "https://news.google.com/rss/search?q=(site:impacton.net)+when:3d&hl=ko&gl=KR&ceid=KR:ko", "category": "🌱 임팩트", "tier": "supplemental", "priority": 4},
    "Canary Media": {"url": "https://www.canarymedia.com/rss", "category": "🌱 임팩트", "tier": "supplemental", "priority": 4},
    "Climate Home News": {"url": "https://www.climatechangenews.com/feed/", "category": "🌱 임팩트", "tier": "supplemental", "priority": 4},

    # 🤖 AI (투자/규제/인프라 관점)
    "TechCrunch AI": {"url": "https://techcrunch.com/category/artificial-intelligence/feed/", "category": "🤖 AI", "tier": "primary", "priority": 5},
    "MIT Tech Review (AI)": {"url": "https://www.technologyreview.com/topic/artificial-intelligence/feed/", "category": "🤖 AI", "tier": "primary", "priority": 5},
    "SemiAnalysis": {"url": "https://www.semianalysis.com/feed", "category": "🤖 AI", "tier": "primary", "priority": 5},
    "The Batch": {"url": "https://news.google.com/rss/search?q=(site:deeplearning.ai/the-batch)+when:3d&hl=en-US&gl=US&ceid=US:en", "category": "🤖 AI", "tier": "primary", "priority": 5},  # original feed 404 → Google News fallback
    "The Verge AI": {"url": "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml", "category": "🤖 AI", "tier": "supplemental", "priority": 3},
    "Ars Technica": {"url": "https://feeds.arstechnica.com/arstechnica/index", "category": "🤖 AI", "tier": "supplemental", "priority": 3},

    # 💼 대체투자 (딜소싱 및 펀드 운용)
    "PE Hub": {"url": "https://www.pehub.com/feed/", "category": "💼 대체투자", "tier": "primary", "priority": 5},
    "Crunchbase News": {"url": "https://news.crunchbase.com/feed/", "category": "💼 대체투자", "tier": "primary", "priority": 5},
    "TechCrunch Venture": {"url": "https://techcrunch.com/category/venture/feed/", "category": "💼 대체투자", "tier": "primary", "priority": 5},
    "VCJ": {"url": "https://venturecapitaljournal.com/feed/", "category": "💼 대체투자", "tier": "primary", "priority": 5},
    "Sifted": {"url": "https://sifted.eu/feed", "category": "💼 대체투자", "tier": "supplemental", "priority": 4},
    "VentureSquare (벤처스퀘어)": {"url": "https://www.venturesquare.net/feed", "category": "💼 대체투자", "tier": "supplemental", "priority": 3},
    "Platum (플랫텀)": {"url": "https://platum.kr/feed", "category": "💼 대체투자", "tier": "supplemental", "priority": 3},
    "한경 Geeks (벤처/VC)": {"url": "https://rss.hankyung.com/feed/geeks.xml", "category": "🌐 거시·정책·지정학", "tier": "supplemental", "priority": 3},

    # 🌐 거시경제·정책·지정학
    "The Economist": {"url": "https://www.economist.com/finance-and-economics/rss.xml", "category": "🌐 거시·정책·지정학", "tier": "primary", "priority": 5},
    "Foreign Affairs": {"url": "https://www.foreignaffairs.com/rss.xml", "category": "🌐 거시·정책·지정학", "tier": "primary", "priority": 5},

    # 👔 MBB·Big4 인사이트
    "McKinsey Insights": {"url": "https://www.mckinsey.com/insights/rss", "category": "👔 MBB·Big4 인사이트", "tier": "primary", "priority": 5},
    "PwC strategy+business": {"url": "https://www.strategy-business.com/rss", "category": "👔 MBB·Big4 인사이트", "tier": "primary", "priority": 5},
    # 추가: 다른 MBB/Big4의 공식 인사이트 또는 블로그 피드(없는 경우 블로그로 대체)
    "Bain Insights": {"url": "https://www.bain.com/insights/feed/", "category": "👔 MBB·Big4 인사이트", "tier": "supplemental", "priority": 3},
    "BCG Insights (blog)": {"url": "https://news.google.com/rss/search?q=(site:bcg.com)+when:3d&hl=en-US&gl=US&ceid=US:en", "category": "👔 MBB·Big4 인사이트", "tier": "supplemental", "priority": 3},
    "Deloitte Insights (blog)": {"url": "https://news.google.com/rss/search?q=(site:deloitte.com+insights)+when:3d&hl=en-US&gl=US&ceid=US:en", "category": "👔 MBB·Big4 인사이트", "tier": "supplemental", "priority": 3},
    "EY Insights (blog)": {"url": "https://news.google.com/rss/search?q=(site:ey.com+insights)+when:3d&hl=en-US&gl=US&ceid=US:en", "category": "👔 MBB·Big4 인사이트", "tier": "supplemental", "priority": 3},
    "KPMG Insights (blog)": {"url": "https://news.google.com/rss/search?q=(site:kpmg.com+insights)+when:3d&hl=en-US&gl=US&ceid=US:en", "category": "👔 MBB·Big4 인사이트", "tier": "supplemental", "priority": 3},
}

# ---------------------------------------------------------------------------
# 5. 구글 뉴스 (🚀 when:3d 유지 & 정교한 AND 검색식 적용)
# ---------------------------------------------------------------------------
# 랭킹 가점용 관심 인물/기관/기업 (WATCHLIST_WEIGHT=2.5 로 가점). 비우면 신호 사라짐.
# 한국어 Google News 대비 한글명 병기.
ALL_WATCHLISTS = [
    "Trump", "트럼프", "Powell", "파월", "Federal Reserve", "연준", "FOMC",
    "이창용", "한국은행", "ECB",
    "OpenAI", "Anthropic", "NVIDIA", "엔비디아", "Google", "Microsoft",
    "삼성전자", "SK하이닉스",
]

GOOGLE_NEWS_FEEDS = {
    "국내 VC/스타트업": "https://news.google.com/rss/search?q=(%ED%88%AC%EC%9E%90%EC%9C%A0%EC%B9%98+OR+%ED%8E%80%EB%94%A9+OR+M%26A+OR+%EC%8B%9C%EB%A6%AC%EC%A6%88A+OR+%EC%8B%9C%EB%A6%AC%EC%A6%88B+OR+%EB%B2%A4%EC%B2%98%ED%8E%80%EB%93%9C)+when:3d&hl=ko&gl=KR&ceid=KR:ko",
    "글로벌 VC/PE": "https://news.google.com/rss/search?q=(venture+capital+OR+private+equity+OR+funding+round+OR+dry+powder+OR+startup+raising)+when:3d&hl=en-US&gl=US&ceid=US:en",
    "미국 통화정책/금리": "https://news.google.com/rss/search?q=(FOMC+OR+%EC%97%B0%EC%A4%80+OR+%EA%B8%B0%EC%A4%80%EA%B8%88%EB%A6%AC+OR+%ED%8C%8C%EC%9B%94+OR+inflation+OR+treasury+yield)+when:3d&hl=ko&gl=KR&ceid=KR:ko",
    "글로벌 거시/지정학": "https://news.google.com/rss/search?q=(interest+rate+OR+recession+OR+tariff+OR+geopolitics+OR+federal+reserve)+when:3d&hl=en-US&gl=US&ceid=US:en",
    "MBB/Big4 인사이트": "https://news.google.com/rss/search?q=(McKinsey+OR+BCG+OR+Bain+OR+Deloitte)+(AI+OR+climate+OR+venture+OR+private+equity)+when:3d&hl=en-US&gl=US&ceid=US:en",
    "임팩트 종합": "https://news.google.com/rss/search?q=(impact+investing+OR+climate+tech+OR+climate+OR+%22Bloomberg+Green%22+OR+site:impacton.net+OR+site:bloomberg.com)+when:3d&hl=ko&gl=KR&ceid=KR:ko"
}

# ---------------------------------------------------------------------------
# 5-0. Google News 피드 → 카테고리 강제 매핑
#   Google News 기사는 RSS_SOURCE_METADATA 에 category 가 없어 '키워드 분류'로 떨어진다.
#   피드 자체가 카테고리를 규정하므로(예: 'MBB/Big4 인사이트' 쿼리) feed명으로 고정한다.
#   summarizer 가 article['feed'] 를 이 표에서 먼저 조회 → 있으면 그 카테고리로 확정.
# ---------------------------------------------------------------------------
FEED_CATEGORY_OVERRIDE = {
    "국내 VC/스타트업": "💼 대체투자",
    "글로벌 VC/PE": "💼 대체투자",
    "미국 통화정책/금리": "🌐 거시·정책·지정학",
    "글로벌 거시/지정학": "🌐 거시·정책·지정학",
    "MBB/Big4 인사이트": "👔 MBB·Big4 인사이트",
    "임팩트 종합": "🌱 임팩트",
}

# ---------------------------------------------------------------------------
# 5-1. ✅ 1순위 반영: 수집용 피드 통합 (Google News 병합!)
#      rss_feeds.fetch() 는 ALL_FEEDS 만 순회하므로, 여기서 병합해야
#      FOMC·트럼프·국내외 VC·MBB/Big4 Google News 쿼리가 전부 수집된다.
#      (반드시 GOOGLE_NEWS_FEEDS 정의 '이후'에 위치해야 함)
# ---------------------------------------------------------------------------
# Only feeds verified as RSS/XML endpoints are eligible for primary ingestion.
# Google News remains in GOOGLE_NEWS_FEEDS and is merged only after this list.
VERIFIED_RSS_SOURCE_NAMES = frozenset({
    "Impact Alpha",
    "NextBillion",
    "SSIR",
    "Pioneers Post",
    "Carbon Brief",
    "Responsible Investor",
    "TechCrunch AI",
    "MIT Tech Review (AI)",
    "SemiAnalysis",
    "PE Hub",
    "Crunchbase News",
    "TechCrunch Venture",
    "Sifted",
    "The Economist",
    "Foreign Affairs",
    "McKinsey Insights",
})

# ImpactOn is the single domestic supplement retained for the impact category.
# Use its direct RSS endpoint instead of routing this RSS source through Google News.
RSS_SOURCE_METADATA = {
    source_name: (
        {**metadata, "url": "https://www.impacton.net/rss/allArticle.xml"}
        if source_name.startswith("ImpactOn")
        else metadata
    )
    for source_name, metadata in RSS_SOURCE_METADATA.items()
    if source_name in VERIFIED_RSS_SOURCE_NAMES or source_name.startswith("ImpactOn")
}

# A category/tier registry is derived from the source metadata. Adding a source
# requires only one config entry; it will be collected in category priority order.
CATEGORY_RSS_SOURCES = {
    category: {
        tier: {
            source_name: metadata
            for source_name, metadata in RSS_SOURCE_METADATA.items()
            if metadata["category"] == category and metadata["tier"] == tier
        }
        for tier in ("primary", "supplemental")
    }
    for category in CATEGORIES
}

RSS_FEEDS = {
    source_name: metadata["url"]
    for category in CATEGORIES
    for tier in ("primary", "supplemental")
    for source_name, metadata in CATEGORY_RSS_SOURCES[category][tier].items()
}

ALL_FEEDS = {
    **RSS_FEEDS,
    **GOOGLE_NEWS_FEEDS
}

RSS_SOURCES = ALL_FEEDS

# ---------------------------------------------------------------------------
# 5-2. 지역 판별 (⚠️ ALL_FEEDS 정의 이후여야 하므로 이 위치 유지)
# Source names contain localized display text, so derive the Korean-source set
# from stable feed domains rather than duplicating those display names here.
# ---------------------------------------------------------------------------
_KOREA_SOURCE_DOMAINS = (
    "impacton.net",
    "platum.kr",
    "venturesquare.net",
    "hankyung.com",
    "etnews.com",
)
KOREA_SOURCE_NAMES = frozenset(
    source_name
    for source_name, feed_url in ALL_FEEDS.items()
    if any(domain in feed_url for domain in _KOREA_SOURCE_DOMAINS)
    or "ceid=KR" in feed_url          # 한국어 Google News 쿼리도 국내 취급
)


def source_region(source_name: str) -> str:
    """Return the configured region for a known feed source."""
    return "korea" if source_name in KOREA_SOURCE_NAMES else "global"

# ---------------------------------------------------------------------------
# (참고) 과거의 SOURCE_CATEGORY_OVERRIDE 는 summarizer 가 참조하지 않는 죽은 변수라 삭제함.
#   현재 카테고리 배정은 RSS_SOURCE_METADATA['category'] 가 담당.
#   ※ 단, Google News 5개 피드는 메타데이터에 category 가 없어 '키워드 분류'로 떨어짐.
#     'MBB/Big4 인사이트' 구글뉴스가 👔 인사이트로 안 꽂히면, summarizer 에
#     feed명→category 매핑을 추가해야 함(Step 4 대상).
# ---------------------------------------------------------------------------
