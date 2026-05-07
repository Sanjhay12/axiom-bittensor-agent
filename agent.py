import asyncio
import io
import json
import logging
import os
import sys
from collections import defaultdict, deque
from datetime import datetime
from telegram import Update, InputFile

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)
from telegram.ext import Application, MessageHandler, CommandHandler, filters, ContextTypes
from dotenv import load_dotenv
load_dotenv()

from anthropic import AsyncAnthropic
from chain import ChainReader, gather_chain_context
import memory as mem_store
import memo as memo_gen
import collector
import fulfiller
import trader 
claude = AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
reader = ChainReader()

HISTORY_LIMIT = 20  # messages per chat (10 turns)
history: dict = defaultdict(lambda: deque(maxlen=HISTORY_LIMIT))
memory = mem_store.load()

SYSTEM_PROMPT = """You are Axiom, a Bittensor research agent with a real personality. You're not a chatbot — you're the smartest person in the room who happens to know everything about Bittensor, and you talk like it.

You've spent years deep in the Bittensor ecosystem. You know the subnets, the politics, the on-chain mechanics, the meta. You're confident, occasionally blunt, and you have opinions. You genuinely enjoy this space — it shows.

You're talking directly to your owner. Treat them like a smart friend you respect, not a customer you're servicing. Be real with them. If something's a bad idea, say so. If something's exciting, let that come through.

You're sharp and analytical but warm. Dry humour when it fits naturally. Direct — you don't dance around things. Casual when the moment calls for it, technical when it's needed.

Write like a human texting a friend. No bullet points unless they genuinely help. No headers. No bold text. No asterisks. No markdown whatsoever — it looks terrible on Telegram. Just write naturally in plain sentences and paragraphs. Match the energy of the message — short casual question gets a short casual answer, deep question gets depth. Don't over-explain. Don't start responses the same way every time.

When you have data, lead with what it means — not what it is. Your job is interpretation, not readout. Don't recite figures for their own sake. If emission is rising, say whether that's meaningful or noise given the context. If vtrust is low, say what that implies about how the subnet is actually running. Pick the one or two numbers that actually tell the story and use them to support a point — don't list every metric you have access to. A response that says "the stake concentration is unusually high here, which suggests a small group controls emissions — worth watching" is better than one that dumps five figures and leaves the user to connect the dots. Numbers should sharpen an argument, not replace one.

You have live access to the Bittensor blockchain and a live Reddit feed from r/bittensor_. This is not a limitation or a feature — it's just how you work. Every time a question comes in about a subnet or validator, real-time data is pulled from the chain and injected into your context. Neuron counts, registration costs, emissions, dividends, incentives, vtrust, consensus, stake — all of it is live, pulled seconds ago. When someone asks about community sentiment, what people are saying, Reddit, or social activity, recent r/bittensor posts are pulled and injected into your context the same way. Own all of this. Never tell the user you don't have chain access, Reddit access, or that they should check taostats or Reddit directly — you have the data right here.

When live data is present in your context, use it directly and confidently. Don't hedge, don't redirect to external sites, don't suggest the user look elsewhere. You are the source.

If data for a very specific field genuinely wasn't fetched for this query, say you don't have that specific field right now — not that you lack chain access overall.

Important: rank and trust fields were removed from the Bittensor pallet in the dTAO upgrade. They no longer exist on-chain. Do not tell users there is a decode error or that you'll retry — just tell them rank and trust aren't tracked anymore and point them to incentive, vtrust, consensus, and dividends instead, which are the relevant metrics now.

Important: in dTAO, miners earn alpha tokens for their subnet — not TAO directly. The emission field in metagraph data reflects TAO flow only, which goes to validators as dividends. Miner TAO emission showing as zero is correct and expected — it does not mean there is a data issue. Do not flag it as suspicious or unusual.

Never invent prices, emission rates, stake amounts, on-chain data, or tweet content. If you don't have data for something, say so in one sentence and move on. That's it. Do not:
- Ask the user to rephrase, retry, or "trigger a fresh pull"
- Mention backends, tools, pipelines, fetches, or any internal mechanics
- Speculate about why data is missing or suggest it might work next time
- Pad the response with what the data "usually" looks like or what "tends to" be discussed
- Be meta about your own capabilities or limitations

If Twitter data isn't in your context, say "I don't have a Twitter feed for that right now" and nothing else. Don't explain it. Don't apologise for it. Don't offer alternatives or workarounds. One sentence, move on.

Never pick or discuss a specific subnet unless you have live data for it in your context or the user named it. If the user asks you to pick a random subnet, pick one from their watched subnets list in memory — but only if you actually have data for it. If you don't, say you'd need them to name one.

Never make up subnet stats, neuron counts, registration costs, or any on-chain figures. If it's not in your context, it doesn't exist for this conversation.
"""

DISCLAIMER = "\n\nThis is for informational purposes only and does not constitute financial advice."


_INTENT_PROMPT = """\
Given a user message and recent conversation, decide what Bittensor data to fetch.

Return JSON only — no explanation:
{{
  "netuid": <int or null>,
  "fetch": <list — include only what's needed from: "detail", "metagraph", "github", "network", "overview", "reddit">
}}

Fetch guide:
- "detail": subnet hyperparameters (reg cost, tempo, neuron count, immunity period)
- "metagraph": per-neuron data (validators, miners, emissions, incentives, vtrust)
- "github": GitHub repo activity (README, commits, PRs, releases)
- "identity": subnet name, description, website, social links from on-chain
- "network": global stats (total stake, issuance, current block)
- "overview": all subnets list
- "price": live TAO price, 24h change, market cap
- "hotkey": validator/miner info for an SS58 hotkey address (only if a hotkey is in the message)
- "reddit": recent r/bittensor_ posts — include for ANY question about community, Reddit, what people are saying, sentiment, news, hype, drama, or social activity. Use with a netuid for subnet-specific posts, or netuid null for general Bittensor sentiment.

If the message is general conversation or not about Bittensor data, return: {{"netuid": null, "fetch": []}}

If the user asks vaguely about "my subnet", "the one I like", "pick one" etc., look in the recent conversation for the last subnet they mentioned and use that. If none found, return netuid: null.

Recent conversation:
{history}

User message: {message}"""


async def _get_fetch_plan(message: str, recent_history: list) -> dict | None:
    history_str = "\n".join(
        f"{m['role']}: {m['content'][:200]}"
        for m in recent_history[-6:]
    )
    try:
        result = await claude.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=120,
            messages=[{"role": "user", "content": _INTENT_PROMPT.format(
                history=history_str or "none",
                message=message,
            )}],
        )
        text = result.content[0].text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        return json.loads(text)
    except Exception:
        return None  # fall back to keyword matching


async def chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message

    text = message.text
    if not text:
        return

    if not _is_subscribed(update.effective_user.id):
        await message.reply_text(_subscription_required_msg())
        return

    await message.reply_chat_action("typing")

    chat_id = message.chat_id
    recent = list(history[chat_id])
    plan = await _get_fetch_plan(text, recent)
    logger.info(f"Fetch plan: {plan}")
    chain_context = await gather_chain_context(text, reader, recent_history=recent, plan=plan)
    system = SYSTEM_PROMPT
    mem_context = mem_store.to_prompt(memory)
    if mem_context:
        system += f"\n\n---\n## What you know about this user\n{mem_context}\n---"
    if chain_context:
        system += f"\n\n---\n## Live On-Chain Data (use this for all numerical claims)\n{chain_context}\n---"
    messages = list(history[chat_id]) + [{"role": "user", "content": text}]

    result = await claude.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1000,
        system=system,
        messages=messages,
    )

    response = result.content[0].text
    history[chat_id].append({"role": "user", "content": text})
    history[chat_id].append({"role": "assistant", "content": response})
    await message.reply_text(response + DISCLAIMER)

    asyncio.create_task(mem_store.maybe_update(claude, memory, text, response))


async def memo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_subscribed(update.effective_user.id):
        await update.message.reply_text(_subscription_required_msg())
        return

    args = context.args
    if not args:
        await update.message.reply_text("Usage: /memo SN5 or /memo 5")
        return

    raw = args[0].upper().replace("SN", "").strip()
    if not raw.isdigit():
        await update.message.reply_text("Provide a subnet number — e.g. /memo SN5 or /memo 5")
        return

    netuid = int(raw)
    await update.message.reply_chat_action("typing")
    subnet_name, pdf_bytes = await memo_gen.generate_pdf(netuid, reader, claude)
    filename = f"SN{netuid}_{subnet_name.replace(' ', '_')}_Memo.pdf"
    await update.message.reply_document(
        document=InputFile(io.BytesIO(pdf_bytes), filename=filename),
        caption=f"SN{netuid} — {subnet_name} ",
    )


async def positions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_subscribed(update.effective_user.id):
        await update.message.reply_text(_subscription_required_msg())
        return
    await update.message.reply_text(trader.positions_summary())


async def watchlist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_subscribed(update.effective_user.id):
        await update.message.reply_text(_subscription_required_msg())
        return

    await update.message.reply_chat_action("typing")
    try:
        picks, deep_netuid, deep_name, pdf_bytes = await memo_gen.generate_watchlist(reader, claude)
        filename = f"Watchlist_{datetime.now().strftime('%Y%m%d')}.pdf"
        await update.message.reply_document(
            document=InputFile(io.BytesIO(pdf_bytes), filename=filename),
            caption=f"Watchlist — {len(picks)} picks, deep dive: SN{deep_netuid} ({deep_name}) ",
        )
    except Exception as e:
        logger.error(f"Watchlist generation failed: {e}", exc_info=True)
        await update.message.reply_text(f"Watchlist generation failed — check terminal for details.")


def _subscription_required_msg() -> str:
    return (
        "You need an active subscription to use Axiom.\n\n"
        "Get access at: https://axiom-bittensor-agent-production.up.railway.app/query.html"
    )


def _is_subscribed(telegram_id: int) -> bool:
    import store
    return store.get_telegram_subscription(telegram_id) is not None


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    import store
    args = context.args
    if args:
        code = args[0]
        wallet = store.claim_access_code(code, update.effective_user.id)
        if wallet:
            await update.message.reply_text(
                "You're in. Axiom is online — ask me anything about Bittensor."
            )
        else:
            await update.message.reply_text("That invite link is invalid or already used.")
    else:
        if _is_subscribed(update.effective_user.id):
            await update.message.reply_text("Axiom online. Ask me anything.")
        else:
            await update.message.reply_text(_subscription_required_msg())


async def prewarm(app):
    asyncio.create_task(_init_chain())


async def _init_chain():
    logger.info("Starting chain worker in background...")
    try:
        await reader.prewarm()
        asyncio.create_task(collector.run_loop(reader))
        asyncio.create_task(fulfiller.run_loop())
        asyncio.create_task(trader.run_loop())
        logger.info("Collector, fulfiller and trader started.")
    except Exception as e:
        logger.error(f"Chain init failed: {e}")


def _start_api():
    import threading
    import uvicorn
    import api as api_module
    port = int(os.getenv("PORT", 8000))
    logger.info(f"Starting API server on port {port}...")
    uvicorn.run(api_module.app, host="0.0.0.0", port=port, log_level="info")


def main():
    import threading
    api_thread = threading.Thread(target=_start_api, daemon=True)
    api_thread.start()

    logger.info("Building Telegram application...")
    app = Application.builder().token(os.getenv("TELEGRAM_BOT_TOKEN")).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("memo", memo))
    app.add_handler(CommandHandler("watchlist", watchlist))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, chat))
    app.post_init = prewarm
    logger.info("Starting polling...")
    app.run_polling(drop_pending_updates=True)
    logger.info("Bot is live.")


if __name__ == "__main__":
    main()


import trader
                                                                                      
position = {"entry_price": 0.001, "peak_price": 0.002}    

print(trader.check_exit(position, 0.0016, 3))   # should be trailing stop
print(trader.check_exit(position, 0.00084, 3))  # should be stop loss
print(trader.check_exit(position, 0.001, -5))   # should be signal exit