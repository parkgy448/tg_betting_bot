"""
스포츠 베팅 텔레그램 봇 v5
- 관리자 전용 잠금 / 관리자 추가·제거
- 봇 재시작해도 데이터 유지 (JSON 저장)
- 이벤트 금액 등록
- 통계 대시보드
- 참가자 명단 공개
- 경기 삭제
- 당첨자 재추첨
- 베팅 현황 그래프 (텍스트 막대)

필수 설치: pip install "python-telegram-bot==21.10"
"""

import json
import logging
import os
import random
from functools import wraps
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

# ════════════════════════════════════════════════════
#  환경변수 로드 (.env 파일)
# ════════════════════════════════════════════════════
load_dotenv()

BOT_TOKEN     = os.environ["BOT_TOKEN"]
CHANNEL_ID    = os.environ["CHANNEL_ID"]
ADMIN_CONTACT = os.environ["ADMIN_CONTACT"]
PRIZE_TEXT    = os.getenv("PRIZE_TEXT", "포인트 100,000원")

# 관리자 ID: .env의 ADMIN_IDS="123,456,789" 형식으로 입력
# 봇 실행 후 /myid 로 본인 ID 확인 가능
_raw_ids = os.getenv("ADMIN_IDS", "")
ADMIN_IDS: set = {int(x.strip()) for x in _raw_ids.split(",") if x.strip().isdigit()}

# 데이터 저장 파일 경로 (봇과 같은 폴더에 자동 생성됨)
DATA_FILE = os.getenv("DATA_FILE", "bot_data.json")
# ════════════════════════════════════════════════════

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ─── 전역 상태 ────────────────────────────────────────
games: dict = {}
game_counter = 0
stats = {
    "total_games":    0,
    "total_bettors":  0,
    "total_winners":  0,
    "winner_history": [],
}

# 대화 상태값
WAIT_HOME, WAIT_AWAY, WAIT_DATE, WAIT_TIME, WAIT_PRIZE, WAIT_WINNERS = range(6)


# ════════════════════════════════════════════════════
#  데이터 저장 / 불러오기 (JSON)
# ════════════════════════════════════════════════════

def save_data():
    """games, game_counter, stats, ADMIN_IDS 를 JSON 파일에 저장"""
    serializable_games = {}
    for gid, g in games.items():
        sg = dict(g)
        # tuple → list 변환 (JSON 직렬화용)
        sg["bets"] = {
            k: [[uid, uname] for uid, uname in v]
            for k, v in g["bets"].items()
        }
        serializable_games[gid] = sg

    data = {
        "game_counter": game_counter,
        "games":        serializable_games,
        "stats":        stats,
        "admin_ids":    list(ADMIN_IDS),
    }
    try:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        logger.info("💾 데이터 저장 완료")
    except Exception as e:
        logger.error(f"데이터 저장 실패: {e}")


def load_data():
    """JSON 파일에서 데이터 복원"""
    global games, game_counter, stats, ADMIN_IDS

    if not os.path.exists(DATA_FILE):
        logger.info("저장된 데이터 없음 — 새로 시작")
        return

    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

        game_counter = data.get("game_counter", 0)
        stats.update(data.get("stats", {}))

        # 저장된 관리자 목록 복원 (있을 경우)
        if "admin_ids" in data:
            ADMIN_IDS = set(data["admin_ids"])

        raw_games = data.get("games", {})
        for gid, g in raw_games.items():
            # list → tuple 복원
            g["bets"] = {
                k: [(uid, uname) for uid, uname in v]
                for k, v in g["bets"].items()
            }
            games[gid] = g

        logger.info(f"✅ 데이터 복원 완료 — 경기 {len(games)}개, 관리자 {len(ADMIN_IDS)}명")
    except Exception as e:
        logger.error(f"데이터 불러오기 실패: {e}")


# ════════════════════════════════════════════════════
#  관리자 전용 데코레이터
# ════════════════════════════════════════════════════

def admin_only(func):
    """관리자 ID 가 아니면 명령어 차단"""
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user_id = update.effective_user.id
        if user_id not in ADMIN_IDS:
            await update.message.reply_text(
                "🚫 관리자 전용 명령어입니다.\n"
                "권한이 없습니다.\n\n"
                "내 ID 확인: /myid"
            )
            logger.warning(f"권한 없는 접근 차단: user_id={user_id}")
            return
        return await func(update, context, *args, **kwargs)
    return wrapper


# ════════════════════════════════════════════════════
#  메시지 양식 생성 함수들
# ════════════════════════════════════════════════════

def make_bar(count: int, total: int, length: int = 12) -> str:
    """텍스트 막대그래프 생성"""
    if total == 0:
        filled = 0
    else:
        filled = round(count / total * length)
    pct = round(count / total * 100) if total > 0 else 0
    return f"{'█' * filled}{'░' * (length - filled)} {pct}%"


def make_betting_open_text(game: dict) -> str:
    home_c = len(game["bets"]["home"])
    draw_c = len(game["bets"]["draw"])
    away_c = len(game["bets"]["away"])
    total  = home_c + draw_c + away_c

    return (
        f"📢 {game['home']} vs {game['away']}\n"
        f"\n"
        f"⏰ {game['match_time']}\n"
        f"\n"
        f"🏠 홈 : {game['home']}\n"
        f"vs\n"
        f"✈️ 원정 : {game['away']}\n"
        f"-----------------------------------\n"
        f"🔥결과 적중자 랜덤 {game.get('max_winners', 1)}명 선발 🔥\n"
        f"🚀 {game.get('prize', PRIZE_TEXT)} 지급 !\n"
        f"✅ 배팅은 경기 시작 10분 전까지 가능 !\n"
        f"🧸경기 종료 후 당첨자 채널에 공지 !\n"
        f"🪙당첨자 문의 : {ADMIN_CONTACT}\n"
        f"-----------------------------------\n"
        f"📊 현재 참가 현황  (총 {total}명)\n"
        f"🏠 홈 승  : {make_bar(home_c, total)} ({home_c}명)\n"
        f"⚖️ 무승부 : {make_bar(draw_c, total)} ({draw_c}명)\n"
        f"✈️ 원정 승: {make_bar(away_c, total)} ({away_c}명)"
    )

def make_betting_closed_text(game: dict) -> str:
    home_c = len(game["bets"]["home"])
    draw_c = len(game["bets"]["draw"])
    away_c = len(game["bets"]["away"])
    total  = home_c + draw_c + away_c

    return (
        f"📢 {game['home']} vs {game['away']}\n"
        f"\n"
        f"⏰ {game['match_time']}\n"
        f"\n"
        f"🏠 홈 : {game['home']}\n"
        f"vs\n"
        f"✈️ 원정 : {game['away']}\n"
        f"-----------------------------------\n"
        f"🛑 베팅이 마감되었습니다.\n"
        f"-----------------------------------\n"
        f"📊 최종 참가 현황  (총 {total}명)\n"
        f"🏠 홈 승  : {make_bar(home_c, total)} ({home_c}명)\n"
        f"⚖️ 무승부 : {make_bar(draw_c, total)} ({draw_c}명)\n"
        f"✈️ 원정 승: {make_bar(away_c, total)} ({away_c}명)"
    )

def _winner_label(game: dict, winner: str) -> str:
    return {
        "home": f"홈 승 ({game['home']})",
        "draw": "무승부",
        "away": f"원정 승 ({game['away']})"
    }[winner]

def make_result_text(game: dict, winner: str) -> str:
    return (
        f"🎉 경기 결과가 확정되었습니다.\n\n"
        f"({game['home']}) VS ({game['away']})\n"
        f"-----------------------------------\n"
        f"경기 결과: {_winner_label(game, winner)} !!"
    )

def make_winner_text(game: dict, winner: str, winner_names: list) -> str:
    count = len(winner_names)
    winners_str = "\n".join(f"{i+1}. @{name} : {game.get('prize', PRIZE_TEXT)}" for i, name in enumerate(winner_names))
    return (
        f"🏆 당첨자 발표 !\n"
        f"({game['home']}) VS ({game['away']})\n"
        f"-----------------------------------\n"
        f"경기 결과: {_winner_label(game, winner)} !!\n"
        f"당첨자 : {count}명\n"
        f"{winners_str}\n\n"
        f"당첨자 문의 : {ADMIN_CONTACT}"
    )

def make_no_winner_text(game: dict, winner: str) -> str:
    return (
        f"😅 당첨자 없음\n\n"
        f"({game['home']}) VS ({game['away']})\n"
        f"-----------------------------------\n"
        f"경기 결과: {_winner_label(game, winner)} !!\n\n"
        f"해당 결과에 베팅한 참가자가 없습니다."
    )

def make_keyboard(game_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🏠 홈 승",   callback_data=f"bet_{game_id}_home"),
        InlineKeyboardButton("⚖️ 무승부",  callback_data=f"bet_{game_id}_draw"),
        InlineKeyboardButton("✈️ 원정 승", callback_data=f"bet_{game_id}_away"),
    ]])


# ════════════════════════════════════════════════════
#  /myid — 본인 ID 확인 (관리자 등록용)
# ════════════════════════════════════════════════════

async def myid_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid      = update.effective_user.id
    is_admin = "✅ 관리자입니다." if uid in ADMIN_IDS else "❌ 관리자가 아닙니다."
    await update.message.reply_text(
        f"🪪 내 텔레그램 ID: {uid}\n"
        f"{is_admin}\n\n"
        f"관리자로 등록하려면 app.py 의\n"
        f"ADMIN_IDS 에 이 숫자를 추가하세요."
    )


# ════════════════════════════════════════════════════
#  /start — 시작 인사
# ════════════════════════════════════════════════════

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "안녕하세요! 🎰 스포츠 베팅 봇입니다.\n\n"
        "/help 를 입력하면 전체 사용법을 볼 수 있어요!\n"
        "내 ID 확인: /myid"
    )


# ════════════════════════════════════════════════════
#  /help — 사용법 (관리자는 추가 명령어 표시)
# ════════════════════════════════════════════════════

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid      = update.effective_user.id
    is_admin = uid in ADMIN_IDS

    base = (
        "📖 사용법 안내\n"
        "━━━━━━━━━━━━━━━━━━━\n\n"
        "【 통계 대시보드 】\n"
        "/stats\n"
        "→ 누적 경기 수, 참가자 수, 최근 당첨자 내역\n\n"
        "【 내 ID 확인 】\n"
        "/myid\n"
        "→ 내 텔레그램 ID 확인 (관리자 등록용)\n"
    )

    admin_section = (
        "\n\n🔐 관리자 전용 명령어\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        "【 경기 등록 】\n"
        "/newgame\n"
        "→ 홈팀 / 원정팀 / 날짜 / 시간 / 이벤트 상품 / 당첨자 수(1~10)\n"
        "   순서로 입력 → 채널에 베팅 공지 자동 게시\n\n"
        "【 결과 발표 】\n"
        "/result <game_id> <결과>\n"
        "→ 예시:\n"
        "   /result 1 home  ← 홈팀 승\n"
        "   /result 1 draw  ← 무승부\n"
        "   /result 1 away  ← 원정팀 승\n\n"
        "【 베팅 마감 】\n"
        "/close <game_id>\n"
        "→ 예시: /close 1\n\n"
        "【 경기 목록 】\n"
        "/games  → 전체 경기 목록 + game_id 확인\n\n"
        "【 경기 삭제 】\n"
        "/delete <game_id>\n"
        "→ 경기 + 채널 메시지 삭제\n"
        "→ 예시: /delete 1\n\n"
        "【 참가자 명단 】\n"
        "/members <game_id>\n"
        "/members <game_id> home|draw|away\n\n"
        "【 당첨자 재추첨 】\n"
        "/reroll <game_id>\n"
        "→ 결과 발표된 경기에서 재추첨\n"
        "→ 예시: /reroll 1\n\n"
        "【 관리자 관리 】\n"
        "/adminlist           → 관리자 목록 확인\n"
        "/addadmin <id>       → 관리자 추가\n"
        "/removeadmin <id>    → 관리자 제거\n\n"
        "【 취소 】\n"
        "/cancel  → /newgame 도중 취소\n\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        "💡 game_id 모를 때: /games"
    )

    await update.message.reply_text(base + (admin_section if is_admin else ""))


# ════════════════════════════════════════════════════
#  /newgame 대화 핸들러 (관리자 전용)
# ════════════════════════════════════════════════════

@admin_only
async def newgame_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🏠 홈팀 이름을 입력하세요.\n예) 고양 소노"
    )
    return WAIT_HOME

async def got_home(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["home"] = update.message.text.strip()
    await update.message.reply_text(
        "✈️ 원정팀 이름을 입력하세요.\n예) 서울 삼성"
    )
    return WAIT_AWAY

async def got_away(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["away"] = update.message.text.strip()
    await update.message.reply_text(
        "📅 경기 날짜를 입력하세요.\n예) 2026-02-19"
    )
    return WAIT_DATE

async def got_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["date"] = update.message.text.strip()
    await update.message.reply_text(
        "⏰ 경기 시간을 입력하세요.\n예) 19:00"
    )
    return WAIT_TIME

async def got_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["time"] = update.message.text.strip()
    await update.message.reply_text(
        "🚀 이벤트 상품 / 금액을 입력하세요.\n\n"
        "예) 포인트 100,000원\n"
        "예) 상품권 5만원\n"
        "예) 스타벅스 아이스아메리카노"
    )
    return WAIT_PRIZE

async def got_prize(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["prize"] = update.message.text.strip()
    await update.message.reply_text(
        "🏆 당첨자 수를 입력하세요. (1 ~ 10)\n\n"
        "예) 1\n"
        "예) 3"
    )
    return WAIT_WINNERS

async def got_winners(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = update.message.text.strip()
    if not raw.isdigit() or not (1 <= int(raw) <= 10):
        await update.message.reply_text(
            "⚠️ 1 ~ 10 사이의 숫자를 입력해주세요.\n예) 1"
        )
        return WAIT_WINNERS

    global game_counter
    game_counter += 1
    game_id = str(game_counter)

    home        = context.user_data["home"]
    away        = context.user_data["away"]
    date_str    = context.user_data["date"]
    time_str    = context.user_data["time"]
    prize       = context.user_data["prize"]
    max_winners = int(raw)
    match_time  = f"{date_str} {time_str}"

    games[game_id] = {
        "home":          home,
        "away":          away,
        "match_time":    match_time,
        "prize":         prize,
        "max_winners":   max_winners,
        "bets":          {"home": [], "draw": [], "away": []},
        "message_id":    None,
        "extra_msg_ids": [],
        "closed":        False,
        "result":        None,
    }

    game = games[game_id]
    msg = await context.bot.send_message(
        chat_id=CHANNEL_ID,
        text=make_betting_open_text(game),
        reply_markup=make_keyboard(game_id),
    )
    games[game_id]["message_id"] = msg.message_id
    save_data()  # 💾 저장

    await update.message.reply_text(
        f"✅ 경기 등록 완료!\n\n"
        f"🆔 game_id: {game_id}\n"
        f"🚀 이벤트 상품: {prize}\n"
        f"🏆 당첨자 수: {max_winners}명\n"
        f"📢 채널에 베팅 공지를 올렸습니다.\n\n"
        f"[ 결과 입력 명령어 ]\n"
        f"/result {game_id} home  ← 홈팀 승\n"
        f"/result {game_id} draw  ← 무승부\n"
        f"/result {game_id} away  ← 원정팀 승\n\n"
        f"[ 베팅만 먼저 마감할 때 ]\n"
        f"/close {game_id}"
    )
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ 취소되었습니다.")
    return ConversationHandler.END


# ════════════════════════════════════════════════════
#  베팅 버튼 콜백 (누구나 가능)
# ════════════════════════════════════════════════════

async def bet_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query

    _, game_id, choice = query.data.split("_", 2)

    if game_id not in games:
        await query.answer("❌ 존재하지 않는 경기입니다.", show_alert=True)
        return

    game = games[game_id]

    if game["closed"]:
        await query.answer("🚫 베팅이 마감되었습니다.", show_alert=True)
        return

    user_id  = query.from_user.id
    username = query.from_user.username or query.from_user.first_name

    # 중복 베팅 방지
    all_bettors = game["bets"]["home"] + game["bets"]["draw"] + game["bets"]["away"]
    if any(u[0] == user_id for u in all_bettors):
        await query.answer("⚠️ 이미 베팅하셨습니다!", show_alert=True)
        return

    game["bets"][choice].append((user_id, username))
    label = {"home": "🏠 홈 승", "draw": "⚖️ 무승부", "away": "✈️ 원정 승"}[choice]

    await query.answer(
        f"✅ {label} 베팅 완료되었습니다!\n"
        f"경기 결과를 기다려 주세요 🎰",
        show_alert=True
    )

    save_data()  # 💾 저장

    # 채널 메시지 현황 업데이트
    try:
        await context.bot.edit_message_text(
            chat_id=CHANNEL_ID,
            message_id=game["message_id"],
            text=make_betting_open_text(game),
            reply_markup=make_keyboard(game_id),
        )
    except Exception as e:
        logger.warning(f"현황 업데이트 실패 (무시 가능): {e}")


# ════════════════════════════════════════════════════
#  /close — 베팅 수동 마감 (관리자 전용)
# ════════════════════════════════════════════════════

@admin_only
async def close_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if len(args) != 1:
        await update.message.reply_text(
            "사용법: /close <game_id>\n"
            "예시: /close 1\n\n"
            "game_id 모를 때: /games"
        )
        return

    game_id = args[0]
    if game_id not in games:
        await update.message.reply_text(
            "❌ 존재하지 않는 game_id 입니다.\n"
            "/games 로 목록을 확인하세요."
        )
        return

    game = games[game_id]
    if game["closed"]:
        await update.message.reply_text("이미 마감된 경기입니다.")
        return

    game["closed"] = True
    save_data()  # 💾 저장

    try:
        await context.bot.edit_message_text(
            chat_id=CHANNEL_ID,
            message_id=game["message_id"],
            text=make_betting_closed_text(game),
        )
    except Exception as e:
        logger.warning(f"마감 업데이트 실패: {e}")

    await update.message.reply_text(
        f"🛑 game_id {game_id} 베팅 마감 완료!\n\n"
        f"결과 입력:\n"
        f"/result {game_id} home  ← 홈팀 승\n"
        f"/result {game_id} draw  ← 무승부\n"
        f"/result {game_id} away  ← 원정팀 승"
    )


# ════════════════════════════════════════════════════
#  /result — 결과 발표 + 당첨자 추첨 (관리자 전용)
# ════════════════════════════════════════════════════

@admin_only
async def result_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if len(args) != 2:
        await update.message.reply_text(
            "사용법: /result <game_id> <결과>\n\n"
            "예시:\n"
            "/result 1 home  ← 홈팀 승\n"
            "/result 1 draw  ← 무승부\n"
            "/result 1 away  ← 원정팀 승\n\n"
            "game_id 모를 때: /games"
        )
        return

    game_id = args[0]
    winner  = args[1].lower()

    if game_id not in games:
        await update.message.reply_text(
            "❌ 존재하지 않는 game_id 입니다.\n"
            "/games 로 목록을 확인하세요."
        )
        return
    if winner not in ("home", "draw", "away"):
        await update.message.reply_text(
            "❌ 결과값이 올바르지 않습니다.\n"
            "home / draw / away 중 하나를 입력하세요.\n\n"
            "예시: /result 1 home"
        )
        return

    game = games[game_id]
    game["closed"] = True

    # 1) 베팅 메시지 마감
    try:
        await context.bot.edit_message_text(
            chat_id=CHANNEL_ID,
            message_id=game["message_id"],
            text=make_betting_closed_text(game),
        )
    except Exception as e:
        logger.warning(f"마감 처리 실패: {e}")

    # 2) 결과 발표 메시지 — ID 저장
    result_msg = await context.bot.send_message(
        chat_id=CHANNEL_ID,
        text=make_result_text(game, winner),
    )
    game.setdefault("extra_msg_ids", []).append(result_msg.message_id)

    # 3) 당첨자 추첨
    candidates = game["bets"][winner]

    if not candidates:
        no_winner_msg = await context.bot.send_message(
            chat_id=CHANNEL_ID,
            text=make_no_winner_text(game, winner),
        )
        game["extra_msg_ids"].append(no_winner_msg.message_id)
        game["result"] = winner
        save_data()  # 💾 저장
        await update.message.reply_text(
            "✅ 결과 발표 완료!\n"
            "해당 결과에 베팅한 참가자가 없어 당첨자가 없습니다."
        )
        return

    max_w = game.get("max_winners", 1)
    pick_count = min(max_w, len(candidates))
    picked = random.sample(candidates, pick_count)
    winner_names = [name for _, name in picked]

    # 통계 업데이트
    stats["total_games"]   += 1
    stats["total_bettors"] += sum(len(game["bets"][k]) for k in ("home", "draw", "away"))
    stats["total_winners"] += pick_count
    for wname in winner_names:
        stats["winner_history"].append({
            "game":   f"{game['home']} vs {game['away']}",
            "winner": wname,
            "prize":  game.get("prize", PRIZE_TEXT),
            "result": _winner_label(game, winner),
        })
    game["result"] = winner

    winner_msg = await context.bot.send_message(
        chat_id=CHANNEL_ID,
        text=make_winner_text(game, winner, winner_names),
    )
    game["extra_msg_ids"].append(winner_msg.message_id)
    save_data()  # 💾 저장
    names_str = ", ".join(f"@{n}" for n in winner_names)
    await update.message.reply_text(
        f"✅ 결과 발표 및 추첨 완료!\n"
        f"🏆 당첨자 ({pick_count}명): {names_str}"
    )


# ════════════════════════════════════════════════════
#  /games — 경기 목록 확인 (관리자 전용)
# ════════════════════════════════════════════════════

@admin_only
async def games_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not games:
        await update.message.reply_text(
            "등록된 경기가 없습니다.\n"
            "/newgame 으로 경기를 등록해보세요!"
        )
        return

    lines = ["📋 경기 목록\n━━━━━━━━━━━━━━━━━━━\n"]
    for gid, g in games.items():
        status = "🛑 마감" if g["closed"] else "🟢 진행 중"
        home_c = len(g["bets"]["home"])
        draw_c = len(g["bets"]["draw"])
        away_c = len(g["bets"]["away"])
        total  = home_c + draw_c + away_c
        lines.append(
            f"🆔 game_id: {gid}  |  {status}\n"
            f"   {g['home']} vs {g['away']}\n"
            f"   📅 {g['match_time']}\n"
            f"   참가: 홈 {home_c}명 / 무승부 {draw_c}명 / 원정 {away_c}명 (총 {total}명)\n"
        )
    await update.message.reply_text("\n".join(lines))


# ════════════════════════════════════════════════════
#  /stats — 통계 대시보드 (누구나)
# ════════════════════════════════════════════════════

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    active       = sum(1 for g in games.values() if not g["closed"])
    closed_count = sum(1 for g in games.values() if g["closed"])
    live_bettors = sum(
        len(g["bets"]["home"]) + len(g["bets"]["draw"]) + len(g["bets"]["away"])
        for g in games.values() if not g["closed"]
    )

    lines = [
        "📊 통계 대시보드\n"
        "━━━━━━━━━━━━━━━━━━━\n",
        f"🟢 진행 중인 경기 : {active}개",
        f"🛑 완료된 경기    : {closed_count}개  (누적 {stats['total_games']}회)",
        f"👥 누적 베팅 참가 : {stats['total_bettors']}명",
        f"🏆 누적 당첨자    : {stats['total_winners']}명",
        f"🔥 현재 베팅 중   : {live_bettors}명\n",
    ]

    history = stats["winner_history"]
    if history:
        lines.append("━━━━━━━━━━━━━━━━━━━")
        lines.append("🏅 최근 당첨자 내역\n")
        for i, h in enumerate(reversed(history[-5:]), 1):
            lines.append(
                f"{i}. {h['game']}\n"
                f"   결과: {h['result']}\n"
                f"   당첨: @{h['winner']}  /  {h['prize']}\n"
            )
    else:
        lines.append("아직 완료된 경기가 없습니다.")

    await update.message.reply_text("\n".join(lines))


# ════════════════════════════════════════════════════
#  /members — 베팅 참가자 명단 (관리자 전용)
# ════════════════════════════════════════════════════

@admin_only
async def members_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args

    if not args:
        await update.message.reply_text(
            "사용법:\n"
            "/members <game_id>         ← 전체 명단\n"
            "/members <game_id> home    ← 홈 승 베팅자\n"
            "/members <game_id> draw    ← 무승부 베팅자\n"
            "/members <game_id> away    ← 원정 승 베팅자\n\n"
            "예시: /members 1\n"
            "예시: /members 1 home"
        )
        return

    game_id = args[0]
    side    = args[1].lower() if len(args) >= 2 else "all"

    if game_id not in games:
        await update.message.reply_text(
            "❌ 존재하지 않는 game_id 입니다.\n"
            "/games 로 목록을 확인하세요."
        )
        return
    if side not in ("all", "home", "draw", "away"):
        await update.message.reply_text(
            "❌ 올바른 값: home / draw / away / (없으면 전체)\n"
            "예시: /members 1 home"
        )
        return

    game  = games[game_id]
    title = f"{game['home']} vs {game['away']} ({game['match_time']})"

    def fmt_list(label: str, bettors: list) -> str:
        if not bettors:
            return f"{label} : 없음"
        names = "\n".join(f"  {i+1}. @{u[1]}" for i, u in enumerate(bettors))
        return f"{label} ({len(bettors)}명)\n{names}"

    lines = [
        f"👥 베팅 참가자 명단\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"📢 {title}\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
    ]

    if side == "all":
        lines.append(fmt_list("🏠 홈 승", game["bets"]["home"]))
        lines.append("")
        lines.append(fmt_list("⚖️ 무승부", game["bets"]["draw"]))
        lines.append("")
        lines.append(fmt_list("✈️ 원정 승", game["bets"]["away"]))
        total = sum(len(game["bets"][k]) for k in ("home", "draw", "away"))
        lines.append(f"\n합계 : {total}명")
    elif side == "home":
        lines.append(fmt_list("🏠 홈 승", game["bets"]["home"]))
    elif side == "draw":
        lines.append(fmt_list("⚖️ 무승부", game["bets"]["draw"]))
    elif side == "away":
        lines.append(fmt_list("✈️ 원정 승", game["bets"]["away"]))

    await update.message.reply_text("\n".join(lines))


# ════════════════════════════════════════════════════
#  /delete — 경기 삭제 (관리자 전용)
#  사용법: /delete <game_id>
# ════════════════════════════════════════════════════

@admin_only
async def delete_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if len(args) != 1:
        await update.message.reply_text(
            "사용법: /delete <game_id>\n"
            "예시: /delete 1\n\n"
            "⚠️ 베팅 공지 + 결과 + 당첨자 메시지가 모두 삭제됩니다!\n"
            "game_id 모를 때: /games"
        )
        return

    game_id = args[0]
    if game_id not in games:
        await update.message.reply_text(
            "❌ 존재하지 않는 game_id 입니다.\n"
            "/games 로 목록을 확인하세요."
        )
        return

    game  = games[game_id]
    title = f"{game['home']} vs {game['away']} ({game['match_time']})"

    # 삭제 대상: 베팅 공지 + 결과/당첨자/재추첨 메시지 전부
    ids_to_delete = []
    if game.get("message_id"):
        ids_to_delete.append(game["message_id"])
    ids_to_delete.extend(game.get("extra_msg_ids", []))

    deleted = 0
    failed  = 0
    for mid in ids_to_delete:
        try:
            await context.bot.delete_message(chat_id=CHANNEL_ID, message_id=mid)
            deleted += 1
        except Exception as e:
            logger.warning(f"메시지 삭제 실패 (id={mid}): {e}")
            failed += 1

    del games[game_id]
    save_data()

    status = f"채널 메시지 {deleted}개 삭제 완료"
    if failed:
        status += f"\n⚠️ {failed}개 삭제 실패 (이미 삭제됐거나 봇 권한 부족)"

    await update.message.reply_text(
        f"🗑️ 경기 삭제 완료!\n\n"
        f"삭제된 경기: {title}\n"
        f"{status}"
    )


# ════════════════════════════════════════════════════
#  /addadmin — 관리자 추가 (관리자 전용)
#  사용법: /addadmin <user_id>
# ════════════════════════════════════════════════════

@admin_only
async def addadmin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if len(args) != 1 or not args[0].isdigit():
        await update.message.reply_text(
            "사용법: /addadmin <user_id>\n"
            "예시: /addadmin 123456789\n\n"
            "💡 추가할 사람의 ID는 /myid 로 확인"
        )
        return

    new_id = int(args[0])
    if new_id in ADMIN_IDS:
        await update.message.reply_text(
            f"⚠️ {new_id} 는 이미 관리자입니다."
        )
        return

    ADMIN_IDS.add(new_id)
    save_data()  # 💾 저장

    await update.message.reply_text(
        f"✅ 관리자 추가 완료!\n\n"
        f"추가된 ID: {new_id}\n"
        f"현재 관리자 수: {len(ADMIN_IDS)}명"
    )
    logger.info(f"관리자 추가: {new_id} (by {update.effective_user.id})")


# ════════════════════════════════════════════════════
#  /removeadmin — 관리자 제거 (관리자 전용)
#  사용법: /removeadmin <user_id>
# ════════════════════════════════════════════════════

@admin_only
async def removeadmin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if len(args) != 1 or not args[0].isdigit():
        await update.message.reply_text(
            "사용법: /removeadmin <user_id>\n"
            "예시: /removeadmin 123456789\n\n"
            "현재 관리자 목록: /adminlist"
        )
        return

    target_id = int(args[0])
    requester = update.effective_user.id

    if target_id not in ADMIN_IDS:
        await update.message.reply_text(
            f"❌ {target_id} 는 관리자가 아닙니다."
        )
        return

    if target_id == requester:
        await update.message.reply_text(
            "⚠️ 자기 자신은 제거할 수 없습니다."
        )
        return

    if len(ADMIN_IDS) <= 1:
        await update.message.reply_text(
            "⚠️ 관리자가 1명뿐이라 제거할 수 없습니다.\n"
            "먼저 다른 관리자를 추가하세요."
        )
        return

    ADMIN_IDS.discard(target_id)
    save_data()  # 💾 저장

    await update.message.reply_text(
        f"✅ 관리자 제거 완료!\n\n"
        f"제거된 ID: {target_id}\n"
        f"현재 관리자 수: {len(ADMIN_IDS)}명"
    )
    logger.info(f"관리자 제거: {target_id} (by {requester})")


# ════════════════════════════════════════════════════
#  /adminlist — 관리자 목록 확인 (관리자 전용)
# ════════════════════════════════════════════════════

@admin_only
async def adminlist_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lines = ["👑 현재 관리자 목록\n━━━━━━━━━━━━━━━━━━━\n"]
    for i, uid in enumerate(sorted(ADMIN_IDS), 1):
        me = " ← 나" if uid == update.effective_user.id else ""
        lines.append(f"{i}. {uid}{me}")
    lines.append(f"\n총 {len(ADMIN_IDS)}명")
    await update.message.reply_text("\n".join(lines))


# ════════════════════════════════════════════════════
#  /reroll — 당첨자 재추첨 (관리자 전용)
#  사용법: /reroll <game_id>
# ════════════════════════════════════════════════════

@admin_only
async def reroll_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if len(args) != 1:
        await update.message.reply_text(
            "사용법: /reroll <game_id>\n"
            "예시: /reroll 1\n\n"
            "결과가 발표된 경기에서만 사용 가능합니다."
        )
        return

    game_id = args[0]
    if game_id not in games:
        await update.message.reply_text(
            "❌ 존재하지 않는 game_id 입니다.\n"
            "/games 로 목록을 확인하세요."
        )
        return

    game   = games[game_id]
    winner = game.get("result")

    if not winner:
        await update.message.reply_text(
            "⚠️ 아직 결과가 발표되지 않은 경기입니다.\n"
            "먼저 /result 로 경기 결과를 입력하세요."
        )
        return

    candidates = game["bets"][winner]
    if not candidates:
        await update.message.reply_text(
            "❌ 해당 결과에 베팅한 참가자가 없어 재추첨할 수 없습니다."
        )
        return

    max_w = game.get("max_winners", 1)
    pick_count = min(max_w, len(candidates))
    picked = random.sample(candidates, pick_count)
    new_winner_names = [name for _, name in picked]

    # 채널에 재추첨 결과 발표
    winners_str = "\n".join(
        f"{i+1}. @{name} : {game.get('prize', PRIZE_TEXT)}"
        for i, name in enumerate(new_winner_names)
    )
    reroll_text = (
        f"🔄 당첨자 재추첨 결과\n"
        f"({game['home']}) VS ({game['away']})\n"
        f"-----------------------------------\n"
        f"경기 결과: {_winner_label(game, winner)} !!\n"
        f"재추첨 당첨자 : {pick_count}명\n"
        f"{winners_str}\n\n"
        f"당첨자 문의 : {ADMIN_CONTACT}"
    )

    reroll_msg = await context.bot.send_message(
        chat_id=CHANNEL_ID,
        text=reroll_text,
    )
    game.setdefault("extra_msg_ids", []).append(reroll_msg.message_id)
    save_data()  # 💾 저장
    names_str = ", ".join(f"@{n}" for n in new_winner_names)
    await update.message.reply_text(
        f"✅ 재추첨 완료!\n"
        f"🏆 새 당첨자 ({pick_count}명): {names_str}"
    )


# ════════════════════════════════════════════════════
#  메인
# ════════════════════════════════════════════════════

def main():
    load_data()  # 봇 시작 시 저장된 데이터 복원

    app = Application.builder().token(BOT_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("newgame", newgame_start)],
        states={
            WAIT_HOME:    [MessageHandler(filters.TEXT & ~filters.COMMAND, got_home)],
            WAIT_AWAY:    [MessageHandler(filters.TEXT & ~filters.COMMAND, got_away)],
            WAIT_DATE:    [MessageHandler(filters.TEXT & ~filters.COMMAND, got_date)],
            WAIT_TIME:    [MessageHandler(filters.TEXT & ~filters.COMMAND, got_time)],
            WAIT_PRIZE:   [MessageHandler(filters.TEXT & ~filters.COMMAND, got_prize)],
            WAIT_WINNERS: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_winners)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    app.add_handler(conv)
    app.add_handler(CommandHandler("start",       start))
    app.add_handler(CommandHandler("help",        help_command))
    app.add_handler(CommandHandler("myid",        myid_command))
    app.add_handler(CommandHandler("result",      result_command))
    app.add_handler(CommandHandler("close",       close_command))
    app.add_handler(CommandHandler("games",       games_command))
    app.add_handler(CommandHandler("delete",      delete_command))
    app.add_handler(CommandHandler("stats",       stats_command))
    app.add_handler(CommandHandler("members",     members_command))
    app.add_handler(CommandHandler("reroll",      reroll_command))
    app.add_handler(CommandHandler("addadmin",    addadmin_command))
    app.add_handler(CommandHandler("removeadmin", removeadmin_command))
    app.add_handler(CommandHandler("adminlist",   adminlist_command))
    app.add_handler(CallbackQueryHandler(bet_callback, pattern=r"^bet_"))

    logger.info("✅ 봇 시작!")
    app.run_polling()


if __name__ == "__main__":
    main()