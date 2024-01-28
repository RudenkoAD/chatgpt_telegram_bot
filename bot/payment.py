from telegram import LabeledPrice, Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import CallbackContext
import os
from datetime import date, datetime, timedelta
from database import Database
import config
from config import donates

db = Database()

#grab donate levels from donates.yml

async def generate_payment(update: Update, context: CallbackContext):
  update.callback_query.answer()
  await send_payment_message(#this is bad but it's ok for now, feel free to rewrite ths
    update, 
    context, 
    "Подписка на GPT Platform", 
    "Данная подписка позволяет вам безлимитно пользоваться GPT 3.5, а так же открывает GPT4 и множество других инструментов", 
    "subscription", 
    "Подписка на GPT Platform", 
    49900
    )

async def send_payment_message(update: Update, context: CallbackContext, title, description, payload, name, price):
  await context.bot.send_invoice(
    chat_id = update.effective_chat.id, 
    title=title, 
    description = description, 
    payload = payload,
    provider_token = config.payment_token,
    currency = "RUB",
    prices = [LabeledPrice(name, price)]
    )

async def handle_precheckout(update: Update, context: CallbackContext):
  pre_checkout_query = update.pre_checkout_query
  ok : bool = True
  error_message: str = ""
  """check whether everything is alright here and let the user continue the purchase if so, otherwise abort"""
  #idk what to check yet
  await pre_checkout_query.answer(ok, error_message)
  
async def handle_payment(update: Update, context: CallbackContext) -> None:
    """Confirms the successful payment."""
    payload = update.message.successful_payment.invoice_payload
    user_id = update.effective_user.id
    if payload == "subscription":
      db.set_user_attribute(user_id, "subscription_end", datetime.now() + timedelta(days=31))
    else:
      update.message.reply_text("Что-то пошло не так с обработкой вашего платежа! Пожалуйста, напишите @MrDragonlol")