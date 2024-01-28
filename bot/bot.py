import io
import logging
import asyncio
import traceback
import html
import json
from typing import Tuple
from datetime import datetime
import openai
import re

import telegram
from telegram import (
    Update,
    User,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    BotCommand
)
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackContext,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    AIORateLimiter,
    filters,
    PreCheckoutQueryHandler
)
from telegram.constants import ParseMode, ChatAction

import config
import database
import openai_utils
import search
from payment import handle_precheckout, handle_payment, generate_payment

# setup
db = database.Database()
logger = logging.getLogger(__name__)

user_semaphores = {}
user_tasks = {}

def split_text_into_chunks(text, chunk_size):
    for i in range(0, len(text), chunk_size):
        yield text[i:i + chunk_size]


async def register_user_if_not_exists(update: Update, context: CallbackContext, user: User):
    if not db.check_if_user_exists(user.id):
        db.add_new_user(
            user.id,
            update.message.chat_id,
            username=user.username,
            first_name=user.first_name,
            last_name= user.last_name
        )
        db.start_new_dialog(user.id)

    if db.get_user_attribute(user.id, "current_dialog_id") is None:
        db.start_new_dialog(user.id)

    if user.id not in user_semaphores:
        user_semaphores[user.id] = asyncio.Semaphore(1)

    if db.get_user_attribute(user.id, "current_model") is None:
        db.set_user_attribute(user.id, "current_model", config.models["available_text_models"][0])

    # back compatibility for n_used_tokens field
    n_used_tokens = db.get_user_attribute(user.id, "n_used_tokens")
    if isinstance(n_used_tokens, int) or isinstance(n_used_tokens, float):  # old format
        new_n_used_tokens = {
            "gpt-3.5-turbo": {
                "n_input_tokens": 0,
                "n_output_tokens": n_used_tokens
            }
        }
        db.set_user_attribute(user.id, "n_used_tokens", new_n_used_tokens)

    # voice message transcription
    if db.get_user_attribute(user.id, "n_transcribed_seconds") is None:
        db.set_user_attribute(user.id, "n_transcribed_seconds", 0.0)

    # image generation
    if db.get_user_attribute(user.id, "n_generated_images") is None:
        db.set_user_attribute(user.id, "n_generated_images", 0)


async def is_bot_mentioned(update: Update, context: CallbackContext):
     try:
         message = update.message

         if message.chat.type == "private":
             return True

         if message.text is not None and ("@" + context.bot.username) in message.text:
             return True

         if message.reply_to_message is not None:
             if message.reply_to_message.from_user.id == context.bot.id:
                 return True
     except:
         return True
     else:
         return False


async def start_handle(update: Update, context: CallbackContext):
    await register_user_if_not_exists(update, context, update.message.from_user)
    user_id = update.message.from_user.id

    db.set_user_attribute(user_id, "last_interaction", datetime.now())
    db.start_new_dialog(user_id)

    reply_text = """Привет! 💜

Я — твой помощник на основе искусственного интеллекта 🤖
Моя задача — помочь тебе с решением повседневных задач

По умолчанию используется нейросеть ChatGPT, и ты можешь просто написать свое задание или вопрос в этот чат, можно голосом :)"""

    await update.message.reply_text(reply_text, parse_mode=ParseMode.HTML)
    await menu_handle(update, context)

async def search_handle(update: Update, context: CallbackContext):
    await register_user_if_not_exists(update, context, update.message.from_user)
    user_id = update.message.from_user.id
    db.set_user_attribute(user_id, "last_interaction", datetime.now())

    if update.message.text.find(" ") == -1:
        await update.message.reply_text("Invalid query 🔧")
    else:
        query = update.message.text[update.message.text.find(" ") + 1:]

        await update.message.reply_text(search.search_message(query))

async def retry_handle(update: Update, context: CallbackContext):
    await register_user_if_not_exists(update, context, update.message.from_user)
    if await is_previous_message_not_answered_yet(update, context): return

    user_id = update.message.from_user.id
    db.set_user_attribute(user_id, "last_interaction", datetime.now())

    dialog_messages = db.get_dialog_messages(user_id, dialog_id=None)
    if len(dialog_messages) == 0:
        await update.message.reply_text("No message to retry 🤷‍♂️")
        return

    last_dialog_message = dialog_messages.pop()
    db.set_dialog_messages(user_id, dialog_messages, dialog_id=None)  # last message was removed from the context

    await message_handle(update, context, message=last_dialog_message["user"], use_new_dialog_timeout=False)


async def message_handle(update: Update, context: CallbackContext, message=None, use_new_dialog_timeout=True):
    # check if bot was mentioned (for group chats)
    if not await is_bot_mentioned(update, context):
        return

    # check if message is edited
    if update.edited_message is not None:
        await edited_message_handle(update, context)
        return

    _message = message or update.message.text

    # remove bot mention (in group chats)
    if update.message.chat.type != "private":
        _message = _message.replace("@" + context.bot.username, "").strip()

    await register_user_if_not_exists(update, context, update.message.from_user)
    if await is_previous_message_not_answered_yet(update, context): return

    user_id = update.message.from_user.id
    chat_mode = db.get_user_attribute(user_id, "current_chat_mode")

    if chat_mode == "artist":
        await generate_image_handle(update, context, message=message)
        return

    async def message_handle_fn():
        # new dialog timeout
        if use_new_dialog_timeout:
            if (datetime.now() - db.get_user_attribute(user_id, "last_interaction")).seconds > config.new_dialog_timeout and len(db.get_dialog_messages(user_id)) > 0:
                db.start_new_dialog(user_id)
                await update.message.reply_text(f"Начинаем новый диалог из-за того что с прошлого прошло много времени✅", parse_mode=ParseMode.HTML)
        db.set_user_attribute(user_id, "last_interaction", datetime.now())

        # in case of CancelledError
        n_input_tokens, n_output_tokens = 0, 0
        current_model = db.get_user_attribute(user_id, "current_model")

        try:
            # send placeholder message to user
            placeholder_message = await update.message.reply_text("...")

            # send typing action
            await update.message.chat.send_action(action="typing")

            if _message is None or len(_message) == 0:
                 await update.message.reply_text("Пришло пустое сообщение! Попробуй еще раз", parse_mode=ParseMode.HTML)
                 return

            dialog_messages = db.get_dialog_messages(user_id, dialog_id=None)
            parse_mode = config.chat_modes[chat_mode]["parse_mode"]
            
            if db.is_user_above_limit(user_id):
              text, reply_markup = get_above_limit_answer()
              await context.bot.edit_message_text(text, chat_id=placeholder_message.chat_id, message_id=placeholder_message.message_id, reply_markup = reply_markup, parse_mode=parse_mode)

            chatgpt_instance = openai_utils.ChatGPT(model=current_model)
            if config.enable_message_streaming:
                gen = chatgpt_instance.send_message_stream(_message, dialog_messages=dialog_messages, chat_mode=chat_mode)
            else:
                answer, (n_input_tokens, n_output_tokens), n_first_dialog_messages_removed = await chatgpt_instance.send_message(
                    _message,
                    dialog_messages=dialog_messages,
                    chat_mode=chat_mode
                )

                async def fake_gen():
                    yield "finished", answer, (n_input_tokens, n_output_tokens), n_first_dialog_messages_removed

                gen = fake_gen()

            prev_answer = ""
            async for gen_item in gen:
                status, answer, (n_input_tokens, n_output_tokens), n_first_dialog_messages_removed = gen_item

                answer = answer[:4096]  # telegram message limit

                # update only when 100 new symbols are ready
                if abs(len(answer) - len(prev_answer)) < 100 and status != "finished":
                    continue

                try:
                    await context.bot.edit_message_text(answer, chat_id=placeholder_message.chat_id, message_id=placeholder_message.message_id, parse_mode=parse_mode)
                except telegram.error.BadRequest as e:
                    if str(e).startswith("Message is not modified"):
                        continue
                    else:
                        await context.bot.edit_message_text(answer, chat_id=placeholder_message.chat_id, message_id=placeholder_message.message_id)

                await asyncio.sleep(0.01)  # wait a bit to avoid flooding

                prev_answer = answer

            # update user data
            new_dialog_message = {"user": _message, "bot": answer, "date": datetime.now()}
            db.set_dialog_messages(
                user_id,
                db.get_dialog_messages(user_id, dialog_id=None) + [new_dialog_message],
                dialog_id=None
            )

            db.update_n_used_tokens(user_id, current_model, n_input_tokens, n_output_tokens)

        except asyncio.CancelledError:
            # note: intermediate token updates only work when enable_message_streaming=True (config.yml)
            db.update_n_used_tokens(user_id, current_model, n_input_tokens, n_output_tokens)
            raise

        except Exception as e:
            error_text = f"Something went wrong during completion. Reason: {e}"
            logger.error(error_text)
            await update.message.reply_text(error_text)
            return

        # send message if some messages were removed from the context
        if n_first_dialog_messages_removed > 0:
            if n_first_dialog_messages_removed == 1:
                text = "✍️ <i>Note:</i> Your current dialog is too long, so your <b>first message</b> was removed from the context.\n Send /new command to start new dialog"
            else:
                text = f"✍️ <i>Note:</i> Your current dialog is too long, so <b>{n_first_dialog_messages_removed} first messages</b> were removed from the context.\n Send /new command to start new dialog"
            await update.message.reply_text(text, parse_mode=ParseMode.HTML)

    async with user_semaphores[user_id]:
        task = asyncio.create_task(message_handle_fn())
        user_tasks[user_id] = task

        try:
            await task
        except asyncio.CancelledError:
            await update.message.reply_text("✅ Canceled", parse_mode=ParseMode.HTML)
        else:
            pass
        finally:
            if user_id in user_tasks:
                del user_tasks[user_id]


async def is_previous_message_not_answered_yet(update: Update, context: CallbackContext):
    await register_user_if_not_exists(update, context, update.message.from_user)

    user_id = update.message.from_user.id
    if user_semaphores[user_id].locked():
        text = "⏳ Please <b>wait</b> for a reply to the previous message\n"
        text += "Or you can /cancel it"
        await update.message.reply_text(text, reply_to_message_id=update.message.id, parse_mode=ParseMode.HTML)
        return True
    else:
        return False


async def voice_message_handle(update: Update, context: CallbackContext):
    # check if bot was mentioned (for group chats)
    if not await is_bot_mentioned(update, context):
        return

    await register_user_if_not_exists(update, context, update.message.from_user)
    if await is_previous_message_not_answered_yet(update, context): return

    user_id = update.message.from_user.id
    db.set_user_attribute(user_id, "last_interaction", datetime.now())

    voice = update.message.voice
    voice_file = await context.bot.get_file(voice.file_id)
    
    # store file in memory, not on disk
    buf = io.BytesIO()
    await voice_file.download_to_memory(buf)
    buf.name = "voice.oga"  # file extension is required
    buf.seek(0)  # move cursor to the beginning of the buffer

    transcribed_text = await openai_utils.transcribe_audio(buf)
    text = f"🎤: <i>{transcribed_text}</i>"
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)

    # update n_transcribed_seconds
    db.set_user_attribute(user_id, "n_transcribed_seconds", voice.duration + db.get_user_attribute(user_id, "n_transcribed_seconds"))

    await message_handle(update, context, message=transcribed_text)


async def generate_image_handle(update: Update, context: CallbackContext, message=None):
    await register_user_if_not_exists(update, context, update.message.from_user)
    if await is_previous_message_not_answered_yet(update, context): return

    user_id = update.message.from_user.id
    db.set_user_attribute(user_id, "last_interaction", datetime.now())

    await update.message.chat.send_action(action="upload_photo")

    message = message or update.message.text

    try:
        image_urls = await openai_utils.generate_images(message, n_images=config.return_n_generated_images, size=config.image_size)
    except openai.error.InvalidRequestError as e:
        if str(e).startswith("Your request was rejected as a result of our safety system"):
            text = "🥲 Your request <b>doesn't comply</b> with OpenAI's usage policies.\nWhat did you write there, huh?"
            await update.message.reply_text(text, parse_mode=ParseMode.HTML)
            return
        else:
            raise

    # token usage
    db.set_user_attribute(user_id, "n_generated_images", config.return_n_generated_images + db.get_user_attribute(user_id, "n_generated_images"))

    for i, image_url in enumerate(image_urls):
        await update.message.chat.send_action(action="upload_photo")
        await update.message.reply_photo(image_url, parse_mode=ParseMode.HTML)


async def new_dialog_handle(update: Update, context: CallbackContext):
    await register_user_if_not_exists(update, context, update.message.from_user)
    if await is_previous_message_not_answered_yet(update, context): return

    user_id = update.message.from_user.id
    db.set_user_attribute(user_id, "last_interaction", datetime.now())

    db.start_new_dialog(user_id)
    await update.message.reply_text("Starting new dialog ✅")

    chat_mode = db.get_user_attribute(user_id, "current_chat_mode")
    await update.message.reply_text(f"{config.chat_modes[chat_mode]['welcome_message']}", parse_mode=ParseMode.HTML)


async def cancel_handle(update: Update, context: CallbackContext):
    await register_user_if_not_exists(update, context, update.message.from_user)

    user_id = update.message.from_user.id
    db.set_user_attribute(user_id, "last_interaction", datetime.now())

    if user_id in user_tasks:
        task = user_tasks[user_id]
        task.cancel()
    else:
        await update.message.reply_text("<i>Nothing to cancel...</i>", parse_mode=ParseMode.HTML)


async def show_chat_modes_handle(update: Update, context: CallbackContext):
    await register_user_if_not_exists(update, context, update.message.from_user)
    if await is_previous_message_not_answered_yet(update, context): return

    user_id = update.message.from_user.id
    db.set_user_attribute(user_id, "last_interaction", datetime.now())

    text, reply_markup = get_chat_mode_menu(user_id, 0)
    await update.message.reply_text(text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)


async def show_chat_modes_callback_handle(update: Update, context: CallbackContext):
     await register_user_if_not_exists(update.callback_query, context, update.callback_query.from_user)
     if await is_previous_message_not_answered_yet(update.callback_query, context): return

     user_id = update.callback_query.from_user.id
     db.set_user_attribute(user_id, "last_interaction", datetime.now())

     query = update.callback_query
     await query.answer()

     page_index = int(query.data.split("|")[1])
     if page_index < 0:
         return

     text, reply_markup = get_chat_mode_menu(user_id, page_index)
     try:
         await query.edit_message_text(text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
     except telegram.error.BadRequest as e:
         if str(e).startswith("Message is not modified"):
             pass


async def set_chat_mode_handle(update: Update, context: CallbackContext):
    await register_user_if_not_exists(update.callback_query, context, update.callback_query.from_user)
    user_id = update.callback_query.from_user.id

    query = update.callback_query
    await query.answer()

    chat_mode = query.data.split("|")[1]

    db.set_user_attribute(user_id, "current_chat_mode", chat_mode)
    db.start_new_dialog(user_id)

    await context.bot.send_message(
        update.callback_query.message.chat.id,
        f"{config.chat_modes[chat_mode]['welcome_message']}",
        parse_mode=ParseMode.HTML
    )


async def settings_handle(update: Update, context: CallbackContext):
    await register_user_if_not_exists(update, context, update.message.from_user)
    if await is_previous_message_not_answered_yet(update, context): return

    user_id = update.message.from_user.id
    db.set_user_attribute(user_id, "last_interaction", datetime.now())

    text, reply_markup = get_settings_menu(user_id)
    await update.message.reply_text(text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)


async def set_settings_handle(update: Update, context: CallbackContext):
    await register_user_if_not_exists(update.callback_query, context, update.callback_query.from_user)
    user_id = update.callback_query.from_user.id

    query = update.callback_query
    await query.answer()

    _, model_key = query.data.split("|")
    db.set_user_attribute(user_id, "current_model", model_key)
    db.start_new_dialog(user_id)

    text, reply_markup = get_settings_menu(user_id)
    try:
        await query.edit_message_text(text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
    except telegram.error.BadRequest as e:
        if str(e).startswith("Message is not modified"):
            pass


async def edited_message_handle(update: Update, context: CallbackContext):
    if update.edited_message.chat.type == "private":
        text = "🥲 Unfortunately, message <b>editing</b> is not supported"
        await update.edited_message.reply_text(text, parse_mode=ParseMode.HTML)

async def error_handle(update: Update, context: CallbackContext) -> None:
    logger.error(msg="Exception while handling an update:", exc_info=context.error)

    try:
        # collect error message
        tb_list = traceback.format_exception(None, context.error, context.error.__traceback__)
        tb_string = "".join(tb_list)
        update_str = update.to_dict() if isinstance(update, Update) else str(update)
        message = (
            f"An exception was raised while handling an update\n"
            f"<pre>update = {html.escape(json.dumps(update_str, indent=2, ensure_ascii=False))}"
            "</pre>\n\n"
            f"<pre>{html.escape(tb_string)}</pre>"
        )

        # split text into multiple messages due to 4096 character limit
        for message_chunk in split_text_into_chunks(message, 4096):
            try:
                await context.bot.send_message(update.effective_chat.id, message_chunk, parse_mode=ParseMode.HTML)
            except telegram.error.BadRequest:
                # answer has invalid characters, so we send it without parse_mode
                await context.bot.send_message(update.effective_chat.id, message_chunk)
    except:
        await context.bot.send_message(update.effective_chat.id, "Some error in error handler")

async def post_init(application: Application):
    await application.bot.set_my_commands([
        BotCommand("/new", "Начать новый диалог"),
        BotCommand("/mode", "Выбрать режим чата"),
        BotCommand("/retry", "Перегенерировать последний ответ бота"),
        BotCommand("/help", "Показать справку"),
    ])


def get_above_limit_answer():
  text = "К сожалению, вы превысили свой лимит. Можно повторить завтра, или купить подписку"
  keyboard = []
  keyboard.append([InlineKeyboardButton("Купить подписку", callback_data=f"generate_payment")])
  reply_markup = InlineKeyboardMarkup(keyboard)
  
  return text, reply_markup

def get_main_menu(user_id) -> Tuple[str, InlineKeyboardMarkup]:
  subscribtion_status = db.is_user_subscribed(user_id)
  text_no_subscription = """💜 <b>Сменить нейросеть</b> — использовать другую нейросеть или инструмент (GPT-4, Dalle-3)
💜 <b>Профиль</b> — узнать статус подписки (для расширенного доступа к инструментам)

💜 Некоторые инструменты учитывают весь диалог. Как только захочешь перейти на новую тему или задачу — выбери в меню телеграм <b>Новый диалог</b>
💜 Если не понравился последний результат нейросети, можно пересоздать его, выбрав в меню телеграм <b>Ответь снова</b>

Без подписки тебе доступно лишь:
❤️ 20 запросов в день к chatGPT и другим инструментам
❤️ 3 запроса в день к GPT-4
❤️ 1 запрос в день к Dalle-3
При этом с подпиской все инструменты будут работать на GPT-4"""
  
  text_subscription = """💜 <b>Сменить нейросеть</b> — использовать другую нейросеть или инструмент (GPT-4, Dalle-3)
💜 <b>Профиль</b> — узнать статус подписки (для расширенного доступа к инструментам)

💜 Некоторые инструменты учитывают весь диалог. Как только захочешь перейти на новую тему или задачу — выбери в меню телеграм <b>Новый диалог</b>
💜 Если не понравился последний результат нейросети, можно пересоздать его, выбрав в меню телеграм <b>Ответь снова</b>"""
  text = text_subscription if subscribtion_status else text_no_subscription
  
  keyboard = []
  keyboard.append([InlineKeyboardButton("Инструменты", callback_data="navigate|1")])
  keyboard.append([InlineKeyboardButton("Сменить Нейросеть", callback_data="navigate|2")])
  keyboard.append([InlineKeyboardButton("Профиль", callback_data="navigate|3")])
  
  reply_markup = InlineKeyboardMarkup(keyboard)
  
  return text, reply_markup


def get_chat_mode_menu(user_id, page_index: int) -> Tuple[str, InlineKeyboardMarkup]:
    n_chat_modes_per_page = config.n_chat_modes_per_page
    subscribtion_status = db.is_user_subscribed(user_id)
    text_subscription = """Выбери инструмент из списка ниже (можно листать), либо введи запрос текстом — и я подберу тебе подходящую нейросеть 🔎"""
    text_no_subscription = """Выбери инструмент из списка ниже, либо введи запрос текстом — и я подберу тебе подходящую нейросеть 🔎

Без подписки тебе доступны лишь инструменты не отмеченные красным, с ограниченным числом запросов

Чтобы узнать ограничения, выбери <b>Профиль</b> в меню телеграм"""
    text = text_subscription if subscribtion_status else text_no_subscription

    # buttons
    chat_mode_keys = list(config.chat_modes.keys())
    page_chat_mode_keys = chat_mode_keys[page_index * n_chat_modes_per_page:(page_index + 1) * n_chat_modes_per_page]

    keyboard = []
    for chat_mode_key in page_chat_mode_keys:
        name = config.chat_modes[chat_mode_key]["name"]
        keyboard.append([InlineKeyboardButton(name, callback_data=f"set_chat_mode|{chat_mode_key}")])

    # pagination
    if len(chat_mode_keys) > n_chat_modes_per_page:
        is_first_page = (page_index == 0)
        is_last_page = ((page_index + 1) * n_chat_modes_per_page >= len(chat_mode_keys))

        if is_first_page:
            keyboard.append([
                InlineKeyboardButton("»", callback_data=f"show_chat_modes|{page_index + 1}")
            ])
        elif is_last_page:
            keyboard.append([
                InlineKeyboardButton("«", callback_data=f"show_chat_modes|{page_index - 1}"),
            ])
        else:
            keyboard.append([
                InlineKeyboardButton("«", callback_data=f"show_chat_modes|{page_index - 1}"),
                InlineKeyboardButton("»", callback_data=f"show_chat_modes|{page_index + 1}")
            ])
    keyboard.append([InlineKeyboardButton("Назад", callback_data="navigate|0")])
    reply_markup = InlineKeyboardMarkup(keyboard)

    return text, reply_markup


def get_settings_menu(user_id: int) -> Tuple[str, InlineKeyboardMarkup]:
    current_model = db.get_user_attribute(user_id, "current_model")
    text:str = config.models["info"][current_model]["description"]
    text += "\n\n"
    score_dict = config.models["info"][current_model]["scores"]
    for score_key, score_value in score_dict.items():
        text += "🟢" * score_value + "⚪️" * (5 - score_value) + f" – {score_key}\n\n"

    text += "\nSelect <b>model</b>:"

    # buttons to choose models
    keyboard = []
    for model_key in config.models["available_text_models"]:
        title = config.models["info"][model_key]["name"]
        if model_key == current_model:
            title = "✅ " + title

        keyboard.append(
            [InlineKeyboardButton(title, callback_data=f"set_settings|{model_key}")]
        )
    keyboard.append([InlineKeyboardButton("« Back", callback_data="navigate|0")])
    reply_markup = InlineKeyboardMarkup(keyboard)

    return text, reply_markup


def get_profile(user_id):
  subscribtion_status = db.is_user_subscribed(user_id)
  text_subscription = """💚 Подписка активна

Тебе доступны все инструменты и неограниченное число запросов 💜

Если возникли проблемы или есть вопросы по подписке — пиши @MrDragonlol"""
  text_no_subscription = """❤️ Подписка отсутствует

Без подписки тебе доступно лишь:
💜 20 запросов в день к chatGPT и другим инструментам
💜 3 запроса в день к GPT-4
💜 1 запрос в день к Dalle-3

С подпиской тебе доступно:
💜 Неограниченно запросов к chatGPT, GPT-4, Dalle-3
💜 Расширенный набор инструментов
💜 Все инструменты будут работать на GPT-4, а не chatGPT

Чтобы оформить подписку, нажми на блок для оплаты ниже
Стоимость — 399 рублей в месяц
Если возникли проблемы с оплатой, пиши @MrDragonlol"""
  text = text_subscription if subscribtion_status else text_no_subscription
  keyboard = []
  keyboard.append([InlineKeyboardButton("💜Купить подписку", callback_data="generate_payment")])
  keyboard.append([InlineKeyboardButton("Назад", callback_data="navigate|0")])
  reply_markup = InlineKeyboardMarkup(keyboard)
  return text, reply_markup


def get_balance_menu(user_id):
    db.set_user_attribute(user_id, "last_interaction", datetime.now())

    # count total usage statistics
    total_n_spent_dollars = 0
    total_n_used_tokens = 0

    n_used_tokens_dict = db.get_user_attribute(user_id, "n_used_tokens")
    n_generated_images = db.get_user_attribute(user_id, "n_generated_images")
    n_transcribed_seconds = db.get_user_attribute(user_id, "n_transcribed_seconds")

    details_text = "🏷️ Details:\n"
    for model_key in sorted(n_used_tokens_dict.keys()):
        n_input_tokens, n_output_tokens = n_used_tokens_dict[model_key]["n_input_tokens"], n_used_tokens_dict[model_key]["n_output_tokens"]
        total_n_used_tokens += n_input_tokens + n_output_tokens

        n_input_spent_dollars = config.models["info"][model_key]["price_per_1000_input_tokens"] * (n_input_tokens / 1000)
        n_output_spent_dollars = config.models["info"][model_key]["price_per_1000_output_tokens"] * (n_output_tokens / 1000)
        total_n_spent_dollars += n_input_spent_dollars + n_output_spent_dollars

        details_text += f"- {model_key}: <b>{n_input_spent_dollars + n_output_spent_dollars:.03f}$</b> / <b>{n_input_tokens + n_output_tokens} tokens</b>\n"

    # image generation
    image_generation_n_spent_dollars = config.models["info"]["dalle-2"]["price_per_1_image"] * n_generated_images
    if n_generated_images != 0:
        details_text += f"- DALL·E 2 (image generation): <b>{image_generation_n_spent_dollars:.03f}$</b> / <b>{n_generated_images} generated images</b>\n"

    total_n_spent_dollars += image_generation_n_spent_dollars

    # voice recognition
    voice_recognition_n_spent_dollars = config.models["info"]["whisper"]["price_per_1_min"] * (n_transcribed_seconds / 60)
    if n_transcribed_seconds != 0:
        details_text += f"- Whisper (voice recognition): <b>{voice_recognition_n_spent_dollars:.03f}$</b> / <b>{n_transcribed_seconds:.01f} seconds</b>\n"

    total_n_spent_dollars += voice_recognition_n_spent_dollars


    text = f"You spent <b>{total_n_spent_dollars:.03f}$</b>\n"
    text += f"You used <b>{total_n_used_tokens}</b> tokens\n\n"
    text += details_text

    keyboard = []
    keyboard.append([InlineKeyboardButton("« Back", callback_data="navigate|0")])
    reply_markup = InlineKeyboardMarkup(keyboard)
    return text, reply_markup


def get_help_menu():
  text = """Команды:
⚪ /retry – Перегенерировать последний ответ бота
⚪ /new – Начать новый диалог
⚪ /mode – Выбрать режим чата
⚪ /help – Показать справку

🎨 Генерация изображений по текстовым подсказкам в режиме <b>👩‍🎨 Художник</b>
🎤 Вы можете отправлять <b>голосовые сообщения</b> вместо текста
"""
  keyboard = []
  keyboard.append([InlineKeyboardButton("« Back", callback_data="navigate|0")])
  reply_markup = InlineKeyboardMarkup(keyboard)
  return text, reply_markup
  
  
def get_page(page_index: int, user_id: int) -> Tuple[str, InlineKeyboardMarkup]:
  if page_index == 0:
    return get_main_menu(user_id)
  elif page_index == 1:
    text, keyboard = get_chat_mode_menu(user_id, 0)
  elif page_index == 2:
    text, keyboard = get_settings_menu(user_id)
  elif page_index == 3:
    text, keyboard = get_profile(user_id)
  elif page_index == 4:
    text, keyboard = get_balance_menu(user_id)
  else:
    text, keyboard = get_main_menu(user_id)
  return text, keyboard  


async def navigate_handle(update: Update, context: CallbackContext):
    query = update.callback_query
    user_id = query.from_user.id
    await register_user_if_not_exists(update, context, query.from_user)
    await query.answer()

    page_index = int(query.data.split("|")[1])
    if page_index < 0:
        return

    text, reply_markup = get_page(page_index, user_id)
    
    try:
        await query.edit_message_text(text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
    except telegram.error.BadRequest as e:
        if str(e).startswith("Message is not modified"):
            pass


async def menu_handle(update: Update, context: CallbackContext):
  register_user_if_not_exists(update, context, update.message.from_user)
  user_id = update.message.from_user.id
  text, keyboard = get_main_menu(user_id)
  await update.message.reply_html(text, reply_markup=keyboard)


async def post_init(application: Application):
    await application.bot.set_my_commands([
        BotCommand("/new", "Start new dialog"),
        BotCommand("/mode", "Select chat mode"),
        BotCommand("/retry", "Re-generate response for previous query"),
        BotCommand("/balance", "Show balance"),
        BotCommand("/settings", "Show settings"),
        BotCommand("/help", "Show help message"),
        BotCommand("/search", "Search for the necessary chat model"),
    ])


async def balance_handle(update: Update, context: CallbackContext):
  register_user_if_not_exists(update, context, update.message.from_user)
  text, keyboard = get_balance_menu(update.message.from_user.id)
  await update.message.reply_html(text, reply_markup=keyboard)
  
  
async def help_handle(update: Update, context: CallbackContext):
    await register_user_if_not_exists(update, context, update.message.from_user)
    user_id = update.message.from_user.id
    db.set_user_attribute(user_id, "last_interaction", datetime.now())
    text, keyboard = get_help_menu()
    await update.message.reply_text(text, reply_markup=keyboard, parse_mode=ParseMode.HTML)


def run_bot() -> None:
    application = (
        ApplicationBuilder()
        .token(config.telegram_token)
        .concurrent_updates(True)
        .rate_limiter(AIORateLimiter(max_retries=5))
        .http_version("1.1")
        .get_updates_http_version("1.1")
        .post_init(post_init)
        .build()
    )

    # add handlers
    user_filter = filters.ALL
    if len(config.allowed_telegram_usernames) > 0:
        usernames = [x for x in config.allowed_telegram_usernames if isinstance(x, str)]
        any_ids = [x for x in config.allowed_telegram_usernames if isinstance(x, int)]
        user_ids = [x for x in any_ids if x > 0]
        group_ids = [x for x in any_ids if x < 0]
        user_filter = filters.User(username=usernames) | filters.User(user_id=user_ids) | filters.Chat(chat_id=group_ids)

    #gpt flow handlers
    application.add_handler(CommandHandler("start", start_handle, filters=user_filter))
    application.add_handler(CommandHandler("search", search_handle, filters=user_filter))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & user_filter, message_handle))
    application.add_handler(CommandHandler("retry", retry_handle, filters=user_filter))
    application.add_handler(CommandHandler("new", new_dialog_handle, filters=user_filter))
    application.add_handler(CommandHandler("cancel", cancel_handle, filters=user_filter))
    application.add_handler(MessageHandler(filters.VOICE & user_filter, voice_message_handle))

    application.add_handler(CallbackQueryHandler(show_chat_modes_callback_handle, pattern="^show_chat_modes"))
    
    #setters commands
    application.add_handler(CallbackQueryHandler(set_chat_mode_handle, pattern="^set_chat_mode"))
    application.add_handler(CallbackQueryHandler(set_settings_handle, pattern="^set_settings"))

    #navigation
    application.add_handler(CommandHandler("menu", menu_handle, filters=user_filter))
    application.add_handler(CommandHandler("balance", balance_handle, filters=user_filter))
    application.add_handler(CommandHandler("help", help_handle, filters=user_filter))
    application.add_handler(CallbackQueryHandler(navigate_handle, pattern="^navigate"))

    #donate
    application.add_handler(PreCheckoutQueryHandler(handle_precheckout))
    application.add_handler(CallbackQueryHandler(generate_payment, pattern="^generate_payment"))
    application.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, handle_payment))
    
    application.add_error_handler(error_handle)

    # start the bot
    application.run_polling()


if __name__ == "__main__":
    run_bot()
