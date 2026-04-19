"""
Hermes AI Agent — MiniMax M2.7 powered trading assistant.

Uses the Anthropic-compatible API from MiniMax with function calling
to interact with the Hermes trading system.
"""

import json
from typing import List, Dict, Optional
from hermes.logging_setup import log
from hermes.config import MINIMAX_API_KEY, MINIMAX_BASE_URL, MINIMAX_MODEL
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
    """MiniMax-powered trading agent with function calling."""

    def __init__(self):
        """Initialize the agent with MiniMax Anthropic-compatible client.

        MiniMax requires 'Authorization: Bearer <key>' header.
        We inject it via extra_headers on every API call for max
        compatibility across all anthropic SDK versions.
        """
        if not MINIMAX_API_KEY:
            raise ValueError(
                "MINIMAX_API_KEY not set in .env! "
                "Get one from https://platform.minimax.io"
            )

        import anthropic
        self._auth_headers = {
            "Authorization": f"Bearer {MINIMAX_API_KEY}",
            "anthropic-version": "2023-06-01",
        }
        # Use AsyncAnthropic to avoid blocking the Telegram Bot's event loop
        self.client = anthropic.AsyncAnthropic(
            api_key="not-used",  # SDK requires non-empty, but MiniMax ignores X-Api-Key
            base_url=MINIMAX_BASE_URL,
        )
        self.model = MINIMAX_MODEL
        self.conversations: Dict[str, List[Dict]] = {}
        self.pending_confirmations: Dict[str, Dict] = {}
        log.info(f"[AGENT] Initialized with model={self.model}, base_url={MINIMAX_BASE_URL}")

    def _get_messages(self, chat_id: str) -> List[Dict]:
        """Get conversation history for a chat, create if new."""
        if chat_id not in self.conversations:
            self.conversations[chat_id] = []
        return self.conversations[chat_id]

    def _trim_history(self, chat_id: str, max_messages: int = 40):
        """Keep conversation history manageable."""
        msgs = self.conversations.get(chat_id, [])
        if len(msgs) > max_messages:
            # Keep the last N messages
            self.conversations[chat_id] = msgs[-max_messages:]

    async def chat(self, chat_id: str, user_message: str) -> str:
        """Process a user message and return the agent's response.

        Handles the full function-calling loop:
        1. Send message to MiniMax
        2. If tool_use, execute tool, send result back
        3. Repeat until text response

        Args:
            chat_id: Unique chat identifier
            user_message: The user's message

        Returns:
            The agent's text response
        """
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
            log.error(f"[AGENT] Error in chat: {e}")
            # Remove last message on error to prevent poisoned history
            if messages and messages[-1]["role"] == "user":
                messages.pop()
            return f"⚠️ Error: {str(e)}\n\nCoba lagi ya, mungkin ada masalah koneksi."

    async def _run_agent_loop(self, chat_id: str, system_prompt: str, max_iterations: int = 10) -> str:
        """Run the agent loop with function calling until a text response."""
        messages = self._get_messages(chat_id)

        for iteration in range(max_iterations):
            log.info(f"[AGENT] Iteration {iteration + 1}, messages: {len(messages)}")

            # Call MiniMax API
            response = await self.client.messages.create(
                model=self.model,
                max_tokens=4096,
                system=system_prompt,
                messages=messages,
                tools=TOOLS,
                temperature=0.7,
                extra_headers=self._auth_headers,
            )

            # Process response content blocks
            assistant_content = []
            text_parts = []
            tool_calls = []

            for block in response.content:
                if block.type == "thinking":
                    # MiniMax M2.7 returns thinking blocks — must preserve in history
                    assistant_content.append({
                        "type": "thinking",
                        "thinking": block.thinking,
                    })
                elif block.type == "text":
                    text_parts.append(block.text)
                    assistant_content.append({
                        "type": "text",
                        "text": block.text
                    })
                elif block.type == "tool_use":
                    tool_calls.append(block)
                    assistant_content.append({
                        "type": "tool_use",
                        "id": block.id,
                        "name": block.name,
                        "input": block.input
                    })

            # Add assistant response to history (include ALL blocks per MiniMax docs)
            messages.append({
                "role": "assistant",
                "content": assistant_content
            })

            # If no tool calls, we're done
            if not tool_calls:
                self._trim_history(chat_id)
                return "\n".join(text_parts) if text_parts else "🤔 Hmm, gak ada response. Coba lagi ya."

            # Execute tool calls
            tool_results = []
            for tool_call in tool_calls:
                tool_name = tool_call.name
                tool_input = tool_call.input

                # Check if confirmation is needed
                if tool_name in CONFIRM_REQUIRED:
                    # Store pending and ask for confirmation
                    self.pending_confirmations[chat_id] = {
                        "tool_call": tool_call,
                        "tool_name": tool_name,
                        "tool_input": tool_input,
                        "system_prompt": system_prompt
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

                    # Return text + confirmation
                    prefix = "\n".join(text_parts) + "\n\n" if text_parts else ""
                    return prefix + confirm_msg

                # Execute non-confirmation tools immediately
                log.info(f"[AGENT] Calling tool: {tool_name}({json.dumps(tool_input, ensure_ascii=False)})")
                result = execute_tool(tool_name, tool_input)
                log.info(f"[AGENT] Tool result: {result[:200]}...")

                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tool_call.id,
                    "content": result
                })

            # Add tool results to history
            messages.append({
                "role": "user",
                "content": tool_results
            })

            # Continue loop to get the next response

        return "⚠️ Maaf, terlalu banyak iterasi. Coba pertanyaan yang lebih spesifik ya."

    async def _handle_confirmation(self, chat_id: str, user_message: str) -> str:
        """Handle a confirmation response for a pending trade."""
        pending = self.pending_confirmations.pop(chat_id)
        messages = self._get_messages(chat_id)
        tool_call = pending["tool_call"]
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
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": tool_call.id,
                    "content": result
                }]
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
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": tool_call.id,
                    "content": json.dumps({"cancelled": True, "reason": "User cancelled"})
                }]
            })

            return await self._run_agent_loop(chat_id, system_prompt)

    def reset_conversation(self, chat_id: str):
        """Reset conversation history for a chat."""
        self.conversations[chat_id] = []
        if chat_id in self.pending_confirmations:
            del self.pending_confirmations[chat_id]
        log.info(f"[AGENT] Reset conversation for {chat_id}")
