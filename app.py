"""
Learning Vault — FastAPI バックエンド
対応入力: YouTube字幕 / 汎用URL / テキストメモ / LINE Bot
"""
import os, sqlite3, json, re, hashlib, threading
try:
    import psycopg2
    import psycopg2.extras
except ImportError:
    psycopg2 = None
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, Header
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel
import anthropic
import httpx
from bs4 import BeautifulSoup
from youtube_transcript_api import YouTubeTranscriptApi, NoTranscriptFound, TranscriptsDisabled
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage

load_dotenv()

app = FastAPI(title="Learning Vault")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
app.mount("/static", StaticFiles(directory="."), name="static")

# ── パス設定（クラウド/ローカル自動切替） ──
DATABASE_URL = os.getenv("DATABASE_URL", "")           # Supabase等のPostgres URL
IS_CLOUD     = os.name != 'nt' or bool(DATABASE_URL)   # Linux(Docker)かDB_URL指定でTrue
PH           = "%s" if DATABASE_URL else "?"            # プレースホルダー
DB_PATH      = Path(os.getenv("DB_PATH", "learning.db"))
OBSIDIAN_DIR = Path(os.getenv("OBSIDIAN_DIR", r"C:\Users\akkey\OneDrive\ドキュメント\Obsidian Vault\Training"))
if not IS_CLOUD:
    OBSIDIAN_DIR.mkdir(parents=True, exist_ok=True)

# ── LINE設定 ──
LINE_TOKEN  = os.getenv("LINE_CHANNEL_TOKEN", "")
LINE_SECRET = os.getenv("LINE_CHANNEL_SECRET", "")
line_bot_api = LineBotApi(LINE_TOKEN) if LINE_TOKEN else None
line_handler = WebhookHandler(LINE_SECRET) if LINE_SECRET else None

# ── DB接続ヘルパー ──
def db_connect():
    if DATABASE_URL:
        return psycopg2.connect(DATABASE_URL)
    return sqlite3.connect(DB_PATH)

# ── DB初期化 ──
def init_db():
    if DATABASE_URL:
        con = db_connect()
        cur = con.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS learnings (
                id        SERIAL PRIMARY KEY,
                created   TEXT NOT NULL,
                input     TEXT NOT NULL,
                source    TEXT,
                title     TEXT,
                content   TEXT,
                summary   TEXT,
                insight   TEXT,
                abstract  TEXT,
                tags      TEXT,
                exp_map   TEXT
            )
        """)
        con.commit(); con.close()
    else:
        with sqlite3.connect(DB_PATH) as con:
            con.execute("""
                CREATE TABLE IF NOT EXISTS learnings (
                    id        INTEGER PRIMARY KEY AUTOINCREMENT,
                    created   TEXT NOT NULL,
                    input     TEXT NOT NULL,
                    source    TEXT,
                    title     TEXT,
                    content   TEXT,
                    summary   TEXT,
                    insight   TEXT,
                    abstract  TEXT,
                    tags      TEXT,
                    exp_map   TEXT
                )
            """)
            existing = {r[1] for r in con.execute("PRAGMA table_info(learnings)")}
            for col in ("title", "content", "abstract"):
                if col not in existing:
                    con.execute(f"ALTER TABLE learnings ADD COLUMN {col} TEXT")
init_db()

# ── Claudeクライアント ──
def get_claude():
    key = os.getenv("ANTHROPIC_API_KEY", "")
    if not key or "XXXXXXXX" in key:
        return None
    return anthropic.Anthropic(api_key=key)

# ═══════════════════════════════════════════
#  コンテンツ取得（汎用）
# ═══════════════════════════════════════════

def extract_video_id(url: str) -> str | None:
    for p in [r'youtu\.be/([A-Za-z0-9_-]{11})',
               r'youtube\.com/watch\?.*v=([A-Za-z0-9_-]{11})',
               r'youtube\.com/embed/([A-Za-z0-9_-]{11})']:
        m = re.search(p, url)
        if m: return m.group(1)
    return None

def fetch_youtube_transcript(video_id: str) -> str | None:
    """youtube-transcript-api v1.x（新API）対応。手動字幕→自動生成→翻訳の順で取得"""
    try:
        api = YouTubeTranscriptApi()
        tlist = api.list(video_id)

        # 手動字幕（ja→en）優先、なければ自動生成、最後に任意言語をjaへ翻訳
        transcript = None
        try:
            transcript = tlist.find_manually_created_transcript(['ja', 'en'])
        except Exception:
            try:
                transcript = tlist.find_generated_transcript(['ja', 'en'])
            except Exception:
                # どの言語でもいいから1つ取って、可能なら日本語へ翻訳
                for t in tlist:
                    transcript = t.translate('ja') if t.is_translatable else t
                    break

        if transcript is None:
            return None

        fetched = transcript.fetch()
        text = ' '.join(s.text for s in fetched if s.text.strip())
        text = re.sub(r'\[(音楽|拍手|笑|Music|Applause)\]', '', text)
        text = re.sub(r'\s+', ' ', text).strip()
        return text[:6000] if text else None
    except Exception as e:
        print(f"[youtube] transcript取得失敗 {video_id}: {e}")
        return None

def fetch_url_content(url: str) -> dict:
    """汎用URL → タイトル + 本文テキストを取得"""
    try:
        headers = {"User-Agent": "Mozilla/5.0 (compatible; LearningVault/1.0)"}
        r = httpx.get(url, headers=headers, timeout=10, follow_redirects=True)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, 'html.parser')

        # タイトル
        title = (soup.find('title') or soup.find('h1') or soup.find('meta', property='og:title'))
        title_text = ""
        if title:
            title_text = title.get('content', '') or title.get_text(strip=True)

        # OGP説明
        og_desc = soup.find('meta', property='og:description')
        og_text = og_desc.get('content', '') if og_desc else ''

        # 本文テキスト（スクリプト・スタイル除去）
        for tag in soup(['script','style','nav','footer','header','aside']):
            tag.decompose()
        body = soup.get_text(separator=' ', strip=True)
        body = re.sub(r'\s+', ' ', body)[:3000]

        ok = len((og_text + body).strip()) >= 40
        return {"ok": ok, "title": title_text, "og": og_text, "body": body}
    except Exception as e:
        return {"ok": False, "title": "", "og": "", "body": "", "error": str(e)}

def detect_source(text: str) -> str:
    t = text.lower()
    if extract_video_id(text):          return "YouTube"
    if "vimeo.com" in t:               return "Vimeo"
    if "newspicks.com" in t:           return "NewsPicks"
    if "facebook.com" in t:            return "Facebook"
    if "notion" in t:                  return "Notion"
    if "drive.google" in t:            return "GoogleDrive"
    if "lstep.app" in t or "sakiyomi" in t: return "SAKIYOMI"
    if "line.me" in t:                 return "LINE"
    if "rakumachi" in t:               return "楽町不動産"
    if re.match(r'https?://', t):      return "Web記事"
    return "メモ"

# ═══════════════════════════════════════════
#  分野（サブスク）定義
# ═══════════════════════════════════════════
# 各サブスクの定義（Claudeの分類判断に使う）
STAT_DEFS = {
    "mj":        "MJサロン：不動産投資コミュニティ。物件取得・大家業・客付け・運営の実務全般",
    "terakoya":  "寺子屋大家会：大家会コミュニティ。不動産投資の基礎〜実践、仲間との学び",
    "tomita":    "富田塾：不動産投資塾。融資・規模拡大・出口・経営者視点の戦略",
    "rakumachi": "楽待：楽待の記事・動画。投資家の実例・市場ニュース・物件情報",
    "newspicks": "ニューズピックス：経済・ビジネスニュース。市場・マクロ・業界トレンド",
    "sakiyomi":  "SAKIYOMI先読み：Instagram運用スクール。Reels・台本・コンセプト・フォロワー獲得",
}
STAT_IDS = list(STAT_DEFS.keys())

# detect_sourceの結果 → サブスクIDへの確定マッピング（URLで判別できるもの）
SOURCE_TO_STAT = {
    "NewsPicks":  "newspicks",
    "SAKIYOMI":   "sakiyomi",
    "楽町不動産":  "rakumachi",
    "LINE":       "mj",        # MJサロンはLINE配信
}

# キーワードマッチ（Claude未使用時のフォールバックのみ）
STAT_KEYS = {
    "mj":        ["mj","mjサロン","客付け","管理会社","入居","原状回復"],
    "terakoya":  ["寺子屋","大家会","仲間","初心者"],
    "tomita":    ["富田","融資","銀行","規模拡大","出口","法人","与信"],
    "rakumachi": ["楽待","楽町","物件","利回り","収益物件"],
    "newspicks": ["newspicks","ニューズピックス","経済","金利","市場","ニュース","株","景気"],
    "sakiyomi":  ["sakiyomi","先読み","instagram","インスタ","reels","台本","コンセプト","フォロワー"],
}

def calc_exp_fallback(text: str) -> list:
    """Claude未使用時のキーワードベースサブスク推定"""
    t = text.lower()
    cats = [s for s, keys in STAT_KEYS.items() if any(k.lower() in t for k in keys)]
    return cats or ["rakumachi"]

REFUSAL_MARKERS = [
    "申し訳", "アクセスでき", "アクセスする", "内容を確認することができ",
    "リンクにアクセス", "情報をご提供", "提供いただけ", "できません",
    "I cannot", "I can't", "unable to access", "I'm sorry",
]
def is_refusal(summary: str) -> bool:
    """Claudeが中身を読めず謝罪・依頼で返したケースを検知"""
    s = (summary or "").lower()
    return any(m.lower() in s for m in REFUSAL_MARKERS)

def cats_to_exp(cats: list) -> dict:
    """選ばれた分野に15EXP付与。主分野（先頭）は20EXP"""
    cats = [c for c in cats if c in STAT_IDS][:3]
    if not cats:
        cats = ["shijo"]
    exp = {}
    for i, c in enumerate(cats):
        exp[c] = 20 if i == 0 else 15
    return exp

# ═══════════════════════════════════════════
#  テキスト整形（見出し・箇条書きを改行して構造化）
# ═══════════════════════════════════════════
def tidy_text(s: str) -> str:
    if not s:
        return s
    s = s.replace("\\n", "\n").replace("\\t", " ")
    # 【見出し】は前後で改行。前は空行を1つ入れてセクション感を出す
    s = re.sub(r'\s*【\s*', '\n\n【', s)
    s = re.sub(r'\s*】\s*', '】\n', s)
    # 箇条書き記号 ◆◇ のみ行頭に落とす（・は「イーロン・マスク」等の並列記号なので触らない）
    s = re.sub(r'(?<!\n)\s*([◆◇])', r'\n\1', s)
    # 「1. 」形式の番号付き箇条書きのみ改行（(1)形式は文中なので除外）
    s = re.sub(r'(?<!\n)\s+(\d+\.\s)', r'\n\1', s)
    # 矢印連結（→）はそのまま、過剰な空行・行頭空白を圧縮
    s = re.sub(r'[ \t]+\n', '\n', s)
    s = re.sub(r'\n{3,}', '\n\n', s)
    return s.strip()

# ═══════════════════════════════════════════
#  Claude分析（Haiku・低コスト）
# ═══════════════════════════════════════════
def analyze_with_claude(content: str, source: str, title: str = "") -> dict:
    client = get_claude()
    if not client:
        return {"summary": content[:80], "insight": "（APIキー未設定）",
                "tags": [], "categories": calc_exp_fallback(content)}

    cat_lines = "\n".join(f"  - {k}: {v}" for k, v in STAT_DEFS.items())
    title_line = f"タイトル: {title}\n" if title else ""
    prompt = f"""あなたは、あっきー（29歳・ITコンサル・不動産2棟保有・規模拡大フェーズ・Instagram準備中・目標は30歳で不動産基盤確立→連続起業家）専属の学習アシスタントです。
あっきーが後で「これ何だっけ」と見返したとき、疲れて頭が回らない状態でも一読で完全に理解できるノートを作ってください。

{title_line}ソース: {source}
内容:
{content[:3500]}

【最重要・書き方のルール】
- 「誰が」「何を」「なぜ」を必ず明示する。発言者や登場人物がいれば名前・肩書・立場を書く。
- 専門用語・固有名詞・数字は省略せず、そのうえで必ず噛み砕いて説明する（例：「ARR（年間経常収益。サブスクの年間売上のこと）」）。
- 結論や主張だけでなく、その「根拠・理由・背景」をセットで書く。
- 抽象的なきれいごとは禁止。読んだ人が情景・論理を再構成できる具体性で書く。
- 文字数は気にしなくてよい。短く飾るより、漏れなく伝わることを優先する。

【サブスク分類】下記から最も近いものを1〜2個だけ選ぶ（中心的なものを先頭）:
{cat_lines}

以下のJSON形式のみ返す（前後に余計なテキスト不要・全フィールド必須）:
{{
  "categories": ["サブスクID（mj/terakoya/tomita/rakumachi/newspicks/sakiyomi）を1〜2個"],
  "tags": ["内容を表す具体的なタグを3〜5個"],
  "summary": "【何の話か】を一文で示したあと、【誰が・どんな立場で・何を・なぜ言っている/起きているか】を網羅的に説明する。登場人物が複数なら全員の主張を書く。数字や固有名詞は噛み砕いて。これを読むだけで動画/記事を見直さなくても内容を人に説明できるレベルにする。",
  "insight": "あっきーが具体的に何をすればいいか。『意識すべき』のような精神論は禁止。『次に物件を検討する時に〇〇を確認する』『△△の判断では□□を基準にする』のように、明日から使える行動として書き、なぜそうすべきかの理由も添える。",
  "abstract": "業種や状況が変わっても通用する普遍的な原則・教訓を1〜2文で言い切る。"
}}"""

    msg = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=2000,
        messages=[{"role": "user", "content": prompt}]
    )
    raw = msg.content[0].text.strip()
    # ```json ... ``` のコードフェンスを除去
    raw = re.sub(r'^```(?:json)?\s*', '', raw)
    raw = re.sub(r'\s*```$', '', raw).strip()
    try:
        m = re.search(r'\{.*\}', raw, re.DOTALL)
        # strict=False: 文字列値内の生の改行（Claudeが入れがち）を許容
        d = json.loads(m.group(), strict=False) if m else {}
    except Exception as e:
        print(f"[analyze] JSONパース失敗、再試行: {str(e)[:80]}")
        d = {}
    d.setdefault("summary", raw[:80])
    d.setdefault("insight", "")
    d.setdefault("abstract", "")
    d.setdefault("tags", [])
    # テキスト整形：改行正規化＋【見出し】や箇条書きを必ず改行して構造を見やすく
    for k in ("summary", "insight", "abstract"):
        if isinstance(d.get(k), str):
            d[k] = tidy_text(d[k])
    cats = [c for c in d.get("categories", []) if c in STAT_IDS]
    d["categories"] = cats or calc_exp_fallback(content)
    return d

# ═══════════════════════════════════════════
#  Obsidian保存
# ═══════════════════════════════════════════
def save_to_obsidian(record: dict):
    if IS_CLOUD: return  # クラウドではObsidian保存をスキップ
    date_str = record["created"][:10]
    safe_src = re.sub(r'[\\/:*?"<>|]', '_', record["source"])
    fname = OBSIDIAN_DIR / f"{date_str}_{record['id']:04d}_{safe_src}.md"
    tags_str = " ".join(f"#{t}" for t in json.loads(record.get("tags") or "[]"))
    abstract = record.get("abstract", "")
    abstract_block = f"\n## 🔺 抽象化・原則\n{abstract}\n" if abstract else ""
    content = f"""---
date: {record["created"]}
source: {record["source"]}
title: {record.get("title","")}
---

## 📝 要約
{record["summary"]}

## 💡 あっきーへの示唆
> {record["insight"]}
{abstract_block}
## 🔗 元入力
{record["input"]}

{tags_str}
"""
    fname.write_text(content, encoding="utf-8")

# ═══════════════════════════════════════════
#  共通：記録処理
# ═══════════════════════════════════════════
def process_record(text: str) -> dict:
    """テキスト/URL を受け取り、分析・DB保存・Obsidian保存して結果を返す"""
    source = detect_source(text)
    title = ""
    analyze_target = text
    transcript_fetched = False
    is_url = bool(re.match(r'https?://', text))

    # YouTube
    video_id = extract_video_id(text)
    if video_id:
        transcript = fetch_youtube_transcript(video_id)
        if transcript:
            analyze_target = transcript
            transcript_fetched = True
        else:
            # 字幕が取れない → ゴミを蓄積しない。要点入力を促す
            return {"ok": False, "reason": "no_content",
                    "message": "この動画は字幕が取得できませんでした。観た後に要点をテキストで送ってください（その方が精度も上がります）。"}

    # 汎用URL（YouTube以外）
    elif is_url:
        fetched = fetch_url_content(text)
        if not fetched.get("ok"):
            return {"ok": False, "reason": "no_content",
                    "message": "このURLは本文を取得できませんでした（ログイン必須・JS描画など）。要点をテキストで送ってください。"}
        title = fetched["title"]
        body = (fetched["og"] + " " + fetched["body"]).strip()
        if body:
            analyze_target = body

    result  = analyze_with_claude(analyze_target, source, title)

    # Claudeが「アクセス/分析できない」と返した場合も弾く（ゴミ蓄積防止）
    if is_url and is_refusal(result.get("summary", "")):
        return {"ok": False, "reason": "refusal",
                "message": "内容を解析できませんでした。要点をテキストで送ってください。"}
    cats = result["categories"]
    # URLでサブスクが確定する場合はそれを主分野（先頭）に強制
    forced = SOURCE_TO_STAT.get(source)
    if forced:
        cats = [forced] + [c for c in cats if c != forced]
    exp_map = cats_to_exp(cats)

    created = datetime.now().isoformat(timespec="seconds")
    vals = (created, text, source, title,
            analyze_target[:500],
            result["summary"], result["insight"], result["abstract"],
            json.dumps(result["tags"], ensure_ascii=False),
            json.dumps(exp_map, ensure_ascii=False))
    if DATABASE_URL:
        con = db_connect()
        cur = con.cursor()
        cur.execute(
            f"""INSERT INTO learnings
               (created,input,source,title,content,summary,insight,abstract,tags,exp_map)
               VALUES ({PH},{PH},{PH},{PH},{PH},{PH},{PH},{PH},{PH},{PH}) RETURNING id""",
            vals)
        record_id = cur.fetchone()[0]
        con.commit(); con.close()
    else:
        with sqlite3.connect(DB_PATH) as con:
            cur = con.execute(
                f"""INSERT INTO learnings
                   (created,input,source,title,content,summary,insight,abstract,tags,exp_map)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                vals)
            record_id = cur.lastrowid

    record = {
        "id": record_id, "created": created, "input": text,
        "source": source, "title": title,
        "summary": result["summary"], "insight": result["insight"],
        "abstract": result["abstract"],
        "tags": json.dumps(result["tags"], ensure_ascii=False),
    }
    try: save_to_obsidian(record)
    except Exception as e: print(f"Obsidian保存エラー: {e}")

    return {
        "ok": True, "id": record_id, "source": source, "title": title,
        "transcript_fetched": transcript_fetched,
        "summary": result["summary"],
        "insight": result["insight"],
        "abstract": result["abstract"],
        "tags": result["tags"],
        "exp_map": exp_map,
    }

# ═══════════════════════════════════════════
#  REST API
# ═══════════════════════════════════════════
class RecordIn(BaseModel):
    text: str

@app.post("/api/record")
async def record_learning(body: RecordIn):
    text = body.text.strip()
    if not text:
        raise HTTPException(400, "空のテキストです")
    return process_record(text)

@app.get("/api/learnings")
async def get_learnings(limit: int = 20):
    if DATABASE_URL:
        con = db_connect()
        cur = con.cursor()
        cur.execute("SELECT id,created,input,source,title,content,summary,insight,abstract,tags,exp_map FROM learnings ORDER BY created DESC LIMIT %s", (limit,))
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        con.close()
        return rows
    else:
        with sqlite3.connect(DB_PATH) as con:
            con.row_factory = sqlite3.Row
            rows = con.execute(
                "SELECT * FROM learnings ORDER BY created DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

@app.get("/api/stats")
async def get_stats():
    if DATABASE_URL:
        con = db_connect()
        cur = con.cursor()
        cur.execute("SELECT exp_map FROM learnings")
        rows = cur.fetchall()
        con.close()
    else:
        with sqlite3.connect(DB_PATH) as con:
            rows = con.execute("SELECT exp_map FROM learnings").fetchall()
    totals = {s: 0 for s in STAT_KEYS}
    for (exp_map,) in rows:
        if exp_map:
            for k, v in json.loads(exp_map).items():
                if k in totals: totals[k] += v
    return totals

# ═══════════════════════════════════════════
#  LINE Bot Webhook
# ═══════════════════════════════════════════
@app.post("/line/webhook")
async def line_webhook(request: Request, x_line_signature: str = Header(None)):
    if not line_handler:
        raise HTTPException(503, "LINE未設定")
    body = await request.body()
    try:
        line_handler.handle(body.decode("utf-8"), x_line_signature)
    except InvalidSignatureError:
        raise HTTPException(400, "署名エラー")
    return PlainTextResponse("OK")

if line_handler:
    @line_handler.add(MessageEvent, message=TextMessage)
    def handle_line_message(event):
        text = event.message.text.strip()
        if not text: return
        try:
            result = process_record(text)
            STAT_LABELS = {
                "fudosan":"不動産力","hoken":"保険戦略","shijo":"市場感覚",
                "insta":"Instagram","shikin":"資金調達力","keiei":"経営判断力"
            }
            exp_text = "、".join(
                f"{STAT_LABELS.get(k,k)} +{v}"
                for k, v in result["exp_map"].items()
            )
            reply = (
                f"✅ [{result['source']}] 記録完了\n"
                f"━━━━━━━━━━\n"
                f"📝 {result['summary']}\n\n"
                f"💡 {result['insight']}\n"
                f"━━━━━━━━━━\n"
                f"⚡ {exp_text}"
            )
        except Exception as e:
            reply = f"⚠️ エラーが発生しました: {str(e)[:100]}"

        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))

# ═══════════════════════════════════════════
#  起動
# ═══════════════════════════════════════════
if __name__ == "__main__":
    import uvicorn, socket, webbrowser, threading, time

    def get_ip():
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try: s.connect(("8.8.8.8", 80)); return s.getsockname()[0]
        except: return "127.0.0.1"
        finally: s.close()

    # ngrok でLINE webhookを公開（デフォルト無効。LINE使用時のみ ENABLE_NGROK=1 で起動）
    # ※ ngrokのセッション上限エラーがサーバー本体を巻き込んで落とす事故を防ぐため、
    #    明示的に有効化された時だけ・別スレッドで・全例外を握りつぶして起動する
    ngrok_url = ""
    ngrok_token = os.getenv("NGROK_AUTHTOKEN", "")
    if ngrok_token and os.getenv("ENABLE_NGROK") == "1":
        def _start_ngrok():
            try:
                from pyngrok import ngrok as pyngrok, conf
                conf.get_default().auth_token = ngrok_token
                tunnel = pyngrok.connect(8765, proto="http")
                url = tunnel.public_url.replace("http://", "https://")
                print(f"  LINE Webhook: {url}/line/webhook")
            except Exception as e:
                m = re.search(r"'(https://[^']+)'", str(e))
                if m:
                    print(f"  LINE Webhook (既存): {m.group(1)}/line/webhook")
                else:
                    print(f"[ngrok] 起動失敗（サーバーは続行）: {str(e)[:120]}")
        # 別スレッドでデタッチ。落ちてもメインのuvicornは無傷
        threading.Thread(target=_start_ngrok, daemon=True).start()

    port = int(os.getenv("PORT", 8765))
    print("=" * 55)
    print("  Learning Vault - Startup OK")
    if not IS_CLOUD:
        ip = get_ip()
        print(f"  PC:     http://localhost:{port}/static/launchpad.html")
        print(f"  Mobile: http://{ip}:{port}/static/launchpad.html")
        webbrowser.open(f"http://localhost:{port}/static/launchpad.html")
    print("=" * 55)
    uvicorn.run(app, host="0.0.0.0", port=port)
