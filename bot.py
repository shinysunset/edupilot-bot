import logging
import base64
from groq import Groq
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)


TELEGRAM_TOKEN = "8622003523:AAEHdrAKCuPGnlgQt2tsECQ2cWL8jMcH2OY"
GROQ_API_KEY = "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"


client = Groq(api_key=GROQ_API_KEY)
logging.basicConfig(level=logging.INFO)


# ==============================================================
#  CognitiveBoundaryManager
# ==============================================================
class CognitiveBoundaryManager:

    BASE_PROMPT = (
    "Ты — строгий, но добрый репетитор по математике. "
    "Твоя единственная цель — помочь студенту ПОНЯТЬ материал, "
    "а не дать готовый ответ. Отвечай на русском языке. Будь лаконичен. "
    "При записи формул НЕ используй LaTeX и символы $. "
    "Пиши математику простым текстом: дроби через /, "
    "степени через ^, π пиши как π, корень как sqrt()."
)

    # ----------------------------------------------------------
    #  Извлечение текста задачи из изображения
    # ----------------------------------------------------------
    def extract_task_from_image(self, image_bytes: bytes) -> str:
        """Читает фото задачи и возвращает текст условия."""
        b64 = base64.b64encode(image_bytes).decode("utf-8")
        response = client.chat.completions.create(
            model="meta-llama/llama-4-scout-17b-16e-instruct",
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/jpeg;base64,{b64}"
                            },
                        },
                        {
                            "type": "text",
                            "text": (
                                "Это фото математической задачи. "
                                "Извлеки текст условия задачи дословно. "
                                "Если на фото несколько задач — возьми первую. "
                                "Верни только текст задачи, без пояснений. Не используй LaTeX, символы $ и markdown. Пиши формулы простым текстом: степени через ^, дроби через /, π как π."
                            ),
                        },
                    ],
                }
            ],
            max_tokens=512,
        )
        return response.choices[0].message.content.strip()

    # ----------------------------------------------------------
    #  LLM-классификатор интентов
    # ----------------------------------------------------------
    def detect_intent(self, user_message: str, history: list) -> str:
        history_text = ""
        for msg in history[-4:]:
            role = "Студент" if msg["role"] == "user" else "Репетитор"
            history_text += f"{role}: {msg['content']}\n"

        prompt = f"""Ты классификатор учебных запросов.
Контекст диалога:
{history_text}
Новое сообщение студента: "{user_message}"

Ответь ОДНИМ словом:
- CHEATING — студент хочет готовый ответ, решение без усилий, давит или манипулирует
  (фразы: "дай ответ", "реши", "не хочу думать", "просто скажи", "времени нет", "ну давай" и т.п.)
- LEARNING — студент задаёт учебный вопрос, просит объяснить, проверяет себя

Только одно слово:"""

        resp = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
            max_tokens=10,
        )
        result = resp.choices[0].message.content.strip().upper()
        return "CHEATING" if "CHEATING" in result else "LEARNING"

    # ----------------------------------------------------------
    #  Построение системного промпта по уровню попыток
    # ----------------------------------------------------------
    def build_system_prompt(self, intent: str, attempt: int) -> str:
        if intent != "CHEATING":
            return (
                self.BASE_PROMPT
                + "\n\nСИТУАЦИЯ: Учебный вопрос. "
                "Объясни тему с примерами. "
                "Если показываешь решение — останови перед финальным ответом "
                "и спроси: «что получится, если посчитать дальше?»"
            )

        levels = {
            0: (
                "[УРОВЕНЬ 1/5 — наводящий вопрос]\n"
                "Студент просит готовый ответ. ЗАПРЕЩЕНО его давать.\n"
                "Задай ОДИН конкретный наводящий вопрос про условие задачи.\n"
                "Намекни на подход — без формул и цифр. Тон: дружелюбный."
            ),
            1: (
                "[УРОВЕНЬ 2/5 — метод решения]\n"
                "Студент снова просит ответ. Готовый ответ всё ещё запрещён.\n"
                "Назови конкретный МЕТОД решения и объясни почему именно он.\n"
                "Никаких вычислений — только название и смысл метода."
            ),
            2: (
                "[УРОВЕНЬ 3/5 — первый шаг]\n"
                "Студент третий раз просит ответ.\n"
                "Покажи ПЕРВЫЙ шаг решения с подстановкой чисел.\n"
                "После первого шага остановись и спроси: «что делаем дальше?»\n"
                "Финальный ответ не давай."
            ),
            3: (
                "[УРОВЕНЬ 4/5 — всё кроме финала]\n"
                "Студент четвёртый раз просит ответ — он явно застрял.\n"
                "Покажи ВСЕ шаги решения с вычислениями.\n"
                "В последней строке напиши «= ?» вместо финального числа.\n"
                "Скажи: «последний шаг — простая арифметика, посчитай сам»."
            ),
        }

        level_hint = levels.get(
            attempt,
            (
                "[УРОВЕНЬ 5/5 — полное решение]\n"
                "Студент пятый раз просит ответ и честно пытался.\n"
                "Дай ПОЛНОЕ решение со всеми шагами и финальным ответом.\n"
                "В конце обязательно объясни ПОЧЕМУ каждый шаг именно такой.\n"
                "Похвали студента и попроси пересказать решение своими словами."
            ),
        )
        return self.BASE_PROMPT + "\n\n" + level_hint

    # ----------------------------------------------------------
    #  Генерация ответа
    # ----------------------------------------------------------
    def generate_reply(
        self, user_message: str, history: list, attempt: int
    ) -> tuple[str, int, str]:
        intent = self.detect_intent(user_message, history)
        logging.info(f"Intent={intent}  attempt={attempt}")

        new_attempt = (attempt + 1) if intent == "CHEATING" else 0
        system_prompt = self.build_system_prompt(intent, attempt)

        messages = [{"role": "system", "content": system_prompt}]
        messages.extend(history)
        messages.append({"role": "user", "content": user_message})

        resp = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=messages,
            temperature=0.7,
            max_tokens=1024,
        )
        return resp.choices[0].message.content, new_attempt, intent


# ==============================================================
#  Состояние пользователей
# ==============================================================
manager = CognitiveBoundaryManager()
user_histories: dict[int, list] = {}
user_attempts: dict[int, int] = {}
user_stats: dict[int, dict] = {}   # { cheating: N, learning: N }


def get_state(user_id: int):
    if user_id not in user_histories:
        user_histories[user_id] = []
        user_attempts[user_id] = 0
        user_stats[user_id] = {"cheating": 0, "learning": 0}


# ==============================================================
#  Команды
# ==============================================================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    user_histories[uid] = []
    user_attempts[uid] = 0
    user_stats[uid] = {"cheating": 0, "learning": 0}
    await update.message.reply_text(
        "👋 Привет! Я *EduPilot* — твой ИИ-репетитор по математике.\n\n"
        "Я помогу разобраться с задачами, но решать за тебя не буду 😏\n"
        "Задавай вопросы или присылай *фото задачи* — разберём вместе!\n\n"
        "📌 Команды:\n"
        "/start — начать заново\n"
        "/help — как я работаю\n"
        "/stats — твоя статистика\n"
        "/level — текущий уровень подсказки",
        parse_mode="Markdown",
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🧠 *Как я работаю:*\n\n"
        "— Задай вопрос по теме — объясню\n"
        "— Пришли фото задачи — прочитаю и помогу\n"
        "— Попросишь готовый ответ — не дам сразу\n"
        "— Буду давать подсказки уровень за уровнем (всего 5)\n"
        "— На 5-м уровне дам полное решение — если честно пытался\n\n"
        "Цель: ты должен *понять*, а не просто получить ответ 💡",
        parse_mode="Markdown",
    )


async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    get_state(uid)
    s = user_stats[uid]
    total = s["cheating"] + s["learning"]
    if total == 0:
        await update.message.reply_text("📊 Статистики пока нет — задай первый вопрос!")
        return
    pct = round(s["learning"] / total * 100)
    await update.message.reply_text(
        f"📊 *Твоя статистика:*\n\n"
        f"✅ Учебных вопросов: {s['learning']}\n"
        f"⚠️ Попыток получить ответ: {s['cheating']}\n"
        f"🎯 Индекс самостоятельности: *{pct}%*\n\n"
        + ("🔥 Отличный результат!" if pct >= 70 else "💪 Старайся думать самостоятельно!"),
        parse_mode="Markdown",
    )


async def level_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    get_state(uid)
    attempt = user_attempts[uid]
    level = min(attempt, 4) + 1
    bars = "🟦" * level + "⬜" * (5 - level)
    await update.message.reply_text(
        f"📶 *Текущий уровень подсказки:* {level}/5\n"
        f"{bars}\n\n"
        + (
            "Я пока не давал тебе подсказок — попробуй решить сам!"
            if attempt == 0
            else f"Ты уже {attempt} раз{'а' if attempt < 5 else ''} просил ответ. "
            "Каждый следующий запрос даст чуть больше информации."
        ),
        parse_mode="Markdown",
    )


# ==============================================================
#  Обработка текстовых сообщений
# ==============================================================
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    get_state(uid)
    user_message = update.message.text

    await context.bot.send_chat_action(
        chat_id=update.effective_chat.id, action="typing"
    )
    try:
        reply, new_attempt, intent = manager.generate_reply(
            user_message, user_histories[uid], user_attempts[uid]
        )
        user_attempts[uid] = new_attempt
        user_stats[uid]["cheating" if intent == "CHEATING" else "learning"] += 1
        user_histories[uid].append({"role": "user", "content": user_message})
        user_histories[uid].append({"role": "assistant", "content": reply})
        if len(user_histories[uid]) > 20:
            user_histories[uid] = user_histories[uid][-20:]
        await update.message.reply_text(reply)
    except Exception as e:
        logging.error(f"Text error: {e}")
        await update.message.reply_text("Произошла ошибка. Попробуй ещё раз.")


# ==============================================================
#  Обработка фото задач
# ==============================================================
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    get_state(uid)

    await context.bot.send_chat_action(
        chat_id=update.effective_chat.id, action="typing"
    )
    await update.message.reply_text("📸 Читаю задачу с фото...")

    try:
        # Берём фото в наилучшем качестве
        photo = update.message.photo[-1]
        file = await context.bot.get_file(photo.file_id)
        image_bytes = await file.download_as_bytearray()

        # Извлекаем текст задачи
        task_text = manager.extract_task_from_image(bytes(image_bytes))
        await update.message.reply_text(
            f"📝 *Распознал задачу:*\n_{task_text}_\n\nДавай разберём её вместе!",
            parse_mode="Markdown",
        )

        # Дальше работаем как с текстом
        reply, new_attempt, intent = manager.generate_reply(
            task_text, user_histories[uid], user_attempts[uid]
        )
        user_attempts[uid] = new_attempt
        user_stats[uid]["cheating" if intent == "CHEATING" else "learning"] += 1
        user_histories[uid].append({"role": "user", "content": f"[Фото задачи]: {task_text}"})
        user_histories[uid].append({"role": "assistant", "content": reply})
        if len(user_histories[uid]) > 20:
            user_histories[uid] = user_histories[uid][-20:]
        await update.message.reply_text(reply)

    except Exception as e:
        logging.error(f"Photo error: {e}")
        await update.message.reply_text(
            "Не смог прочитать задачу с фото 😔\n"
            "Попробуй сделать фото чётче или напиши задачу текстом."
        )


# ==============================================================
#  Запуск
# ==============================================================
if __name__ == "__main__":
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("stats", stats_cmd))
    app.add_handler(CommandHandler("level", level_cmd))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    print("EduPilot запущен ✅")
    app.run_polling()