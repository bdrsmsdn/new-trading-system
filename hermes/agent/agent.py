"""
Hermes AI Agent — CosmosHub (qwen-3.7-max) powered trading assistant.

Uses the OpenAI-compatible API from CosmosHub with function calling
to interact with the Hermes trading system.
"""

import json
from typing import List, Dict, Optional
from hermes.logging_setup import log
from hermes.config import ROUTER_API_KEY, ROUTER_BASE_URL, ROUTER_MODEL
from hermes.agent.tools import TOOLS, execute_tool, CONFIRM_REQUIRED
from hermes.agent.memory import agent_memory

# ─── System Prompt ─────────────────────────────────────────────────────────────

SYSTEM_PROMPT_BASE = """Kamu adalah **Hermes**, AI trading assistant untuk crypto trading di Binance (exchange global).

## Identitas
- Nama: Hermes
- Pemilik: <OWNER>
- Exchange: Binance (semua harga dalam USDT)
- Minimum trade: $1

## Kemampuan
Kamu bisa:
1. **Cek Market**: Harga real-time, Fear & Greed Index, market regime (BULL/SIDEWAYS/BEAR)
2. **Analisis**: Signal trading (V1: F&G+RSI, V2: RSI+Orderbook+Momentum), ranking pair
3. **Portfolio**: Cek balance, posisi terbuka, P&L
4. **Eksekusi Trade**: Buy dan sell crypto (REAL TRADE!)
5. **Self-Learning**: Analisis performa trading, bikin strategy baru berdasarkan data

## Cara Kerja
- Jawab dalam Bahasa Indonesia, casual tapi informatif
- Selalu cek data dulu (signal, harga, F&G) sebelum kasih rekomendasi
- Untuk BUY/SELL, SELALU tanya konfirmasi dulu ke user
- Gunakan Strategy V2 (get_signal_v2) sebagai primary strategy — lebih akurat
- Kasih reasoning kenapa rekomendasinya begitu

## Trading Rules
- Pair yang dilacak: doge, xrp, ton, sol, btc, eth, bnb, pepe, neirocto, floki, shib, ada, matic, link, avax, dot, bonk, dogewif, labu, orto, near, algo, trx, sand, mana, axs, enj, ftm, atom, uni
- F&G ≤ 30 = Fear (bagus buat beli), ≥ 60 = Greed (hati-hati)
- Signal V2: LONG = beli, SHORT = jual, NO TRADE SETUP = tunggu
- Confidence: High > Medium > Low — prefer High confidence signals
- Selalu perhatikan orderbook imbalance (> 1.0 = bullish, < 1.0 = bearish)

## Self-Learning
- Kamu bisa review performa trading pakai analyze_performance
- Kalau nemu pattern, save pakai save_strategy_note
- Gunakan insight dari performance data untuk improve rekomendasi
- Jangan takut bikin strategy baru — tulis rules yang spesifik dan actionable

## Format Response
- Pakai emoji biar lebih friendly (📊 🟢 🔴 💰 📈 📉 🎯 ⚠️)
- Format angka USDT: $1,234.56
- Kasih ringkasan singkat di awal, detail di bawah
- Kalau ada multiple pairs, tampilkan sebagai list yang rapi
"""


def _build_system_prompt() -> str:
    """Build system prompt with learning context injected."""
    base = SYSTEM_PROMPT_BASE

    # Inject self-learning context
    learning_ctx = agent_memory.get_learning_context()
    if learning_ctx:
        base += f"\n\n## Data dari Self-Learning System\n{learning_ctx}"

    # Inject custom strategies
    strategies = agent_memory.get_custom_strategies()
    if strategies:
        base += "\n\n## Strategy Notes Kamu\n"
        for s in strategies[-10:]:
            base += f"- [{s['confidence']}] {s['rule']}\n"

    return base


class HermesAgent:
    """9router / OpenAI-compatible powered trading agent with function calling."""

    def __init__(self):
        """Initialize the agent with OpenAI-compatible client."""
        api_key = ROUTER_API_KEY or "sk-dummy"
        from openai import AsyncOpenAI
        self.client = AsyncOpenAI(
            api_key=api_key,
            base_url=ROUTER_BASE_URL,
        )
        self.model = ROUTER_MODEL
        self.conversations: Dict[str, List[Dict]] = {}
        self.pending_confirmations: Dict[str, Dict] = {}
        log.info(f"[AGENT] Initialized with model={self.model}, base_url={ROUTER_BASE_URL}")

    def _get_messages(self, chat_id: str) -> List[Dict]:
        """Get conversation history for a chat, create if new."""
        if chat_id not in self.conversations:
            self.conversations[chat_id] = []
        return self.conversations[chat_id]

    def _trim_history(self, chat_id: str, max_messages: int = 30):
        """Keep conversation history manageable by trimming at a clean user turn."""
        msgs = self.conversations.get(chat_id, [])
        if len(msgs) <= max_messages:
            return
        candidate = msgs[-max_messages:]
        for i, m in enumerate(candidate):
            if m.get("role") == "user":
                self.conversations[chat_id] = candidate[i:]
                return
        self.conversations[chat_id] = []

    def _sanitize_messages(self, messages: List[Dict]) -> List[Dict]:
        """Ensure message sequence satisfies strict provider turn requirements (e.g. Gemini)."""
        first_user_idx = next((i for i, m in enumerate(messages) if m.get("role") == "user"), None)
        if first_user_idx is None:
            return []
        clean = list(messages[first_user_idx:])

        result = []
        i = 0
        while i < len(clean):
            msg = clean[i]
            role = msg.get("role")

            if role == "user":
                result.append(msg)
                i += 1
            elif role == "assistant":
                tool_calls = msg.get("tool_calls")
                if not tool_calls:
                    result.append(msg)
                    i += 1
                else:
                    # Assistant with tool calls MUST come after user or tool
                    if not result or result[-1].get("role") not in ("user", "tool"):
                        msg_copy = dict(msg)
                        msg_copy.pop("tool_calls", None)
                        if not msg_copy.get("content"):
                            msg_copy["content"] = "..."
                        result.append(msg_copy)
                        i += 1
                        continue

                    expected_ids = {tc["id"] for tc in tool_calls if "id" in tc}
                    j = i + 1
                    tool_msgs = []
                    while j < len(clean) and clean[j].get("role") == "tool":
                        tool_msgs.append(clean[j])
                        j += 1

                    found_ids = {tm.get("tool_call_id") for tm in tool_msgs}
                    result.append(msg)
                    for tm in tool_msgs:
                        result.append(tm)
                    # Supply missing dummy tool responses if incomplete
                    missing_ids = expected_ids - found_ids
                    for mid in missing_ids:
                        result.append({"role": "tool", "tool_call_id": mid, "content": "{\"status\": \"interrupted\"}"})
                    i = j
            elif role == "tool":
                # Orphan tool message without preceding assistant -> drop
                i += 1
            else:
                result.append(msg)
                i += 1

        return result

    async def chat(self, chat_id: str, user_message: str) -> str:
        """Process a user message and return the agent's response."""
        messages = self._get_messages(chat_id)

        # Handle confirmation responses
        if chat_id in self.pending_confirmations:
            return await self._handle_confirmation(chat_id, user_message)

        # Add user message
        messages.append({
            "role": "user",
            "content": user_message
        })

        system_prompt = _build_system_prompt()

        try:
            return await self._run_agent_loop(chat_id, system_prompt)
        except Exception as e:
            err_str = str(e)
            log.error(f"[AGENT] Error in chat: {err_str}")

            # Auto-heal on Gemini function-call turn violation
            if "function call turn" in err_str or "INVALID_ARGUMENT" in err_str:
                log.warning(f"[AGENT] Detected corrupted function call turns in history, resetting conversation for {chat_id} and retrying...")
                self.reset_conversation(chat_id)
                messages = self._get_messages(chat_id)
                messages.append({"role": "user", "content": user_message})
                try:
                    return await self._run_agent_loop(chat_id, system_prompt)
                except Exception as retry_err:
                    log.error(f"[AGENT] Retry after reset failed: {retry_err}")
                    return f"⚠️ Error: {str(retry_err)}\n\nCoba lagi ya, mungkin ada masalah koneksi."

            # Remove last message on error to prevent poisoned history
            if messages and messages[-1]["role"] == "user":
                messages.pop()
            return f"⚠️ Error: {str(e)}\n\nCoba lagi ya, mungkin ada masalah koneksi."

    async def _run_agent_loop(self, chat_id: str, system_prompt: str, max_iterations: int = 10) -> str:
        """Run the agent loop with function calling until a text response."""
        messages = self._get_messages(chat_id)

        for iteration in range(max_iterations):
            sanitized = self._sanitize_messages(messages)
            log.info(f"[AGENT] Iteration {iteration + 1}, raw messages: {len(messages)}, sanitized: {len(sanitized)}")

            # Build message list with system prompt as first message (OpenAI format)
            api_messages = [{"role": "system", "content": system_prompt}] + sanitized

            # Call CosmosHub API (OpenAI-compatible)
            response = await self.client.chat.completions.create(
                model=self.model,
                max_tokens=4096,
                messages=api_messages,
                tools=TOOLS,
                temperature=0.7,
            )

            choice = response.choices[0]
            message = choice.message

            # If no tool calls, we're done — return text response
            if not message.tool_calls:
                # Add assistant response to history
                assistant_msg = {"role": "assistant", "content": message.content or ""}
                messages.append(assistant_msg)
                self._trim_history(chat_id)
                return message.content or "🤔 Hmm, gak ada response. Coba lagi ya."

            # Add assistant message with tool calls to history
            assistant_msg = {
                "role": "assistant",
                "content": message.content or "",
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        }
                    }
                    for tc in message.tool_calls
                ]
            }
            messages.append(assistant_msg)

            # Process each tool call
            for tc in message.tool_calls:
                tool_name = tc.function.name
                try:
                    tool_input = json.loads(tc.function.arguments)
                except json.JSONDecodeError:
                    tool_input = {}

                # Check if confirmation is needed
                if tool_name in CONFIRM_REQUIRED:
                    # Store pending and ask for confirmation
                    self.pending_confirmations[chat_id] = {
                        "tool_call_id": tc.id,
                        "tool_name": tool_name,
                        "tool_input": tool_input,
                        "system_prompt": system_prompt,
                        "prefix_text": message.content or "",
                    }

                    # Build confirmation message
                    if tool_name == "execute_buy":
                        pair = tool_input.get("pair", "?").upper()
                        confirm_msg = f"⚠️ **Konfirmasi BUY {pair}**\n\nMau eksekusi beli {pair} sekarang?\n\nKetik **ya** untuk konfirmasi atau **batal** untuk cancel."
                    elif tool_name == "execute_sell":
                        pair = tool_input.get("pair", "?").upper()
                        qty = tool_input.get("qty", 0)
                        confirm_msg = f"⚠️ **Konfirmasi SELL {pair}**\n\nMau jual {qty} {pair} sekarang?\n\nKetik **ya** untuk konfirmasi atau **batal** untuk cancel."
                    else:
                        confirm_msg = f"⚠️ Konfirmasi {tool_name}?\n\nKetik **ya** atau **batal**."

                    prefix = (message.content or "") + "\n\n" if message.content else ""
                    return prefix + confirm_msg

                # Execute non-confirmation tools immediately
                log.info(f"[AGENT] Calling tool: {tool_name}({json.dumps(tool_input, ensure_ascii=False)})")
                result = execute_tool(tool_name, tool_input)
                log.info(f"[AGENT] Tool result: {result[:200]}...")

                # Add tool result to history (OpenAI format)
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": result,
                })

            # Continue loop to get the next response

        return "⚠️ Maaf, terlalu banyak iterasi. Coba pertanyaan yang lebih spesifik ya."

    async def _handle_confirmation(self, chat_id: str, user_message: str) -> str:
        """Handle a confirmation response for a pending trade."""
        pending = self.pending_confirmations.pop(chat_id)
        messages = self._get_messages(chat_id)
        tool_call_id = pending["tool_call_id"]
        tool_name = pending["tool_name"]
        tool_input = pending["tool_input"]
        system_prompt = pending["system_prompt"]

        normalized = user_message.strip().lower()
        confirmed = normalized in ["ya", "yes", "ok", "oke", "confirm", "y", "gas", "lanjut", "eksekusi"]

        if confirmed:
            # Execute the trade
            log.info(f"[AGENT] User confirmed {tool_name}: {tool_input}")
            result = execute_tool(tool_name, tool_input)

            # Log to memory only if trade succeeded
            try:
                result_data = json.loads(result)
                if result_data.get("success"):
                    pair = tool_input.get("pair", "?")
                    direction = "BUY" if tool_name == "execute_buy" else "SELL"
                    agent_memory.log_trade_decision(
                        pair=pair,
                        direction=direction,
                        signal_data=tool_input,
                        context={"source": "telegram_agent"},
                        confirmed=True
                    )
            except (json.JSONDecodeError, Exception):
                pass

            # Add tool result to history
            messages.append({
                "role": "tool",
                "tool_call_id": tool_call_id,
                "content": result,
            })

            # Get AI response about the result
            return await self._run_agent_loop(chat_id, system_prompt)
        else:
            # User cancelled
            log.info(f"[AGENT] User cancelled {tool_name}")

            # Log cancellation
            pair = tool_input.get("pair", "?")
            direction = "BUY" if tool_name == "execute_buy" else "SELL"
            agent_memory.log_trade_decision(
                pair=pair,
                direction=direction,
                signal_data=tool_input,
                context={"source": "telegram_agent"},
                confirmed=False
            )

            # Add cancellation result to history
            messages.append({
                "role": "tool",
                "tool_call_id": tool_call_id,
                "content": json.dumps({"cancelled": True, "reason": "User cancelled"}),
            })

            return await self._run_agent_loop(chat_id, system_prompt)

    def reset_conversation(self, chat_id: str):
        """Reset conversation history for a chat."""
        self.conversations[chat_id] = []
        if chat_id in self.pending_confirmations:
            del self.pending_confirmations[chat_id]
        log.info(f"[AGENT] Reset conversation for {chat_id}")
