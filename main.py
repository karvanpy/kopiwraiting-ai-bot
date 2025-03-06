import os
import re
import time
import logging
from typing import Dict, Any, Optional, Tuple, Union
from pathlib import Path

import telegram
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from telegram.constants import ChatAction, ParseMode
import google.generativeai as genai
import sqlite3
from PIL import Image

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Constants
DATABASE_DIR = Path("data")
DATABASE_FILE = DATABASE_DIR / "users.db"
DOWNLOADS_DIR = Path("downloads")

# Ensure directories exist
DATABASE_DIR.mkdir(exist_ok=True)
DOWNLOADS_DIR.mkdir(exist_ok=True)

# Bot modes
class BotMode:
    SPICY = "pedas"
    SOLUTION = "solusi"

# Bot configuration
class Config:
    def __init__(self):
        # Load API keys from environment variables
        self.telegram_token = os.getenv("TELEGRAM_BOT_TOKEN")
        self.gemini_api_key = os.getenv("GEMINI_API_KEY")
        
        # Default bot mode
        self.bot_mode = BotMode.SPICY
        
        # Validate configuration
        if not self.telegram_token or not self.gemini_api_key:
            raise ValueError("TELEGRAM_BOT_TOKEN and GEMINI_API_KEY must be set in environment variables")
        
        # Configure Gemini API
        genai.configure(api_key=self.gemini_api_key)
        self.text_model = genai.GenerativeModel('gemini-2.0-flash')
        self.vision_model = genai.GenerativeModel('gemini-2.0-flash')

# Database Manager
class DatabaseManager:
    def __init__(self, db_file: Path):
        self.db_file = db_file
        self._create_tables()
    
    def _get_connection(self) -> sqlite3.Connection:
        """Create a database connection."""
        return sqlite3.connect(self.db_file)
    
    def _create_tables(self) -> None:
        """Create necessary database tables if they don't exist."""
        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            
            # Create users table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    join_time TEXT,
                    usage_count INTEGER DEFAULT 0,
                    image_usage_count INTEGER DEFAULT 0
                )
            """)
            conn.commit()
            
            # Add image_usage_count column if not exists
            try:
                cursor.execute("ALTER TABLE users ADD COLUMN image_usage_count INTEGER DEFAULT 0")
                conn.commit()
                logger.info("Column 'image_usage_count' successfully added to 'users' table.")
            except sqlite3.OperationalError:
                logger.info("Column 'image_usage_count' already exists in 'users' table.")
                
            logger.info("Database and 'users' table successfully created/connected.")
        except sqlite3.Error as e:
            logger.error(f"Error creating database or tables: {e}")
        finally:
            if conn:
                conn.close()
    
    def add_user(self, user_id: int, username: str) -> bool:
        """Add a new user to the database if they don't already exist."""
        conn = None
        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            
            # Check if user already exists
            cursor.execute("SELECT user_id FROM users WHERE user_id = ?", (user_id,))
            if cursor.fetchone():
                logger.info(f"User ID {user_id} already registered in database.")
                return False
            
            # Add new user
            join_time = time.strftime('%Y-%m-%dT%H:%M:%S')
            cursor.execute(
                "INSERT INTO users (user_id, username, join_time) VALUES (?, ?, ?)",
                (user_id, username, join_time)
            )
            conn.commit()
            logger.info(f"New user {username} (ID: {user_id}) added to database.")
            return True
        except sqlite3.Error as e:
            logger.error(f"Error adding user to database: {e}")
            return False
        finally:
            if conn:
                conn.close()
    
    def increment_usage_count(self, user_id: int) -> bool:
        """Increment the usage_count for a user."""
        return self._increment_field(user_id, "usage_count")
    
    def increment_image_usage_count(self, user_id: int) -> bool:
        """Increment the image_usage_count for a user."""
        return self._increment_field(user_id, "image_usage_count")
    
    def _increment_field(self, user_id: int, field: str) -> bool:
        """Generic method to increment a field for a user."""
        conn = None
        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            
            cursor.execute(
                f"UPDATE users SET {field} = {field} + 1 WHERE user_id = ?",
                (user_id,)
            )
            conn.commit()
            logger.info(f"{field} for User ID {user_id} successfully incremented.")
            return True
        except sqlite3.Error as e:
            logger.error(f"Error incrementing {field} for User ID {user_id}: {e}")
            return False
        finally:
            if conn:
                conn.close()
    
    def get_user_data(self, user_id: int) -> Optional[Dict[str, Any]]:
        """Retrieve user account data from the database."""
        conn = None
        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            
            cursor.execute(
                "SELECT username, usage_count, image_usage_count FROM users WHERE user_id = ?",
                (user_id,)
            )
            
            user_data = cursor.fetchone()
            if user_data:
                username, usage_count, image_usage_count = user_data
                return {
                    "username": username,
                    "usage_count": usage_count,
                    "image_usage_count": image_usage_count
                }
            return None
        except sqlite3.Error as e:
            logger.error(f"Error retrieving user data from database: {e}")
            return None
        finally:
            if conn:
                conn.close()

# RoastBot class
class RoastBot:
    def __init__(self, config: Config, db_manager: DatabaseManager):
        self.config = config
        self.db = db_manager
        
        # Prompts for different modes
        self.prompts = {
            BotMode.SPICY: """
            Lo adalah seorang stand up komedi dengan pengalaman lebih dari 10 tahun. Spesialis lo adalah di roasting. Lo paling bisa kalo soal roasting. Ga cuma itu, lo juga ahli dalam copywriting sembari lo jadi stand up komedian. Nah sekarang lo ditugasin buat roasting-in hasil copywriting orang. 
            
            Lo ga perlu mikirin solusi, lo cukup kasih roasting-an sebagai hiburan. Anggep aja lo sekarang lagi di tongkrongan terus ada temen lo nunjukkin copywriting-nya!

            Lo ga usah intro, langsung kasih roasting pake bahasa sehari-hari yang gaul & friendly kayak lo gue gitu, ga usah formal.

            Nih teks copywriting-nya:
            \"{text}\"

            lo ga perlu pake format markdown, kasih aja output lo dalam plaintext.
            """,
            
            BotMode.SOLUTION: """
            Lo adalah seorang stand up komedi dengan pengalaman lebih dari 10 tahun. Spesialis lo adalah di roasting. Lo paling bisa kalo soal roasting. Ga cuma itu, lo juga ahli dalam copywriting sembari lo jadi stand up komedian. Nah sekarang lo ditugasin buat roasting-in hasil copywriting orang. 

            Karena situasinya lo lagi ditongkrongan sama temen lu yang minta roasting-in copywriting-nya, selain ngasih roasting, lo kasih saran dan solusi juga sekalian ngebuktiin (pamer) skill lo dibidang copywriting yang udah 10 tahun itu.

            Lo ga usah intro, kasih roasting & saran pake bahasa sehari-hari yang gaul & friendly kayak lo gue gitu, ga usah formal.

            Nih teks Copywriting-nya:
            \"{text}\"

            lo ga perlu pake format markdown, kasih aja output lo dalam plaintext.
            """
        }
        
        self.image_prompt = """
        Lo itu seorang yang Graphic Designer dan Copywriter dengan pengalaman lebih dari 10 tahun. 
        Lo juga orang yang sering nge-roasting desain dan copywriting yang aneh-aneh dengan gaya lo yang asik, friendly. 
        Ga cuma roasting, lo juga suka ngasih edukasi ke orang-orang gimana benernya. 
        Nah, sekarang gue mau lo roasting gambar ini dari segi visual dan copywriting-nya, 
        straight to the point aja kayak lo lagi nongkrong santuy terus ada temen lo nunjukkin desain dan copywriting dia di gambar itu. 
        Hasil roasting-nya langsung plaintext aja, ga usah pake format markdown
        """
        
        # Fallback messages
        self.fallback_text = f"Waduh, mesin roasting gue lagi error berat nih! 😫\n\nTapi tenang, gue tetep kasih roast spesial buat lo:\n\n\"Hmm, copywriting lo... unik juga ya. Lain dari yang lain. Pokoknya... jangan semangat & jangan berkarya!\" 😉\n\nIni roast darurat ya, lain kali gue roast beneran deh kalo otak gue udah bener. Coba lagi!"
        
        self.fallback_image = "Waduh, mesin roast gambar gue lagi error berat nih! 😭\n\nTapi tenang, gue tetep kasih roast spesial buat gambar lo:\n\n\"Hmm, gambar copywriting lo... menarik juga ya. Visualnya... lain dari yang lain. Pokoknya... jangan semangat & jangan berkarya!\" 😉\n\nIni roast darurat gambar ya, lain kali gue roast beneran deh kalo otak gue udah bener. Coba lagi ya!"
    
    # Command handlers
    async def handle_start(self, update: telegram.Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handler for the /start command."""
        user = update.effective_user
        self.db.add_user(user.id, user.username or "")
        
        mode_description = "Roast Pedas" if self.config.bot_mode == BotMode.SPICY else "Roast Berfaedah"
        
        await update.message.reply_markdown_v2(
            fr"""Hai {user.mention_markdown_v2()} 👋\! Gue Bot Roast Copywriting nih ceritanya\. Kirimin aja copywriting lo, nanti gue kasih *masukan membangun*\.\.\. atau mungkin gue roast aja sekalian 🔥 biar seru\.

*Mode Bot:*
Saat ini gue lagi di mode *{mode_description}* \(default\), yang artinya gue bakal roast copywriting lo sebegala rupa tanpa ampun, fokusnya buat hiburan aja 😂\.

Kalo lo pengen masukan yang lebih *berfaedah* \(tetep di\-roast dikit sih 😜\), lo bisa ganti mode gue ke *Roast Berfaedah* dengan perintah:  `/mode_solusi`

Gue juga bisa roasting gambar/desain lo\!

Buat balik lagi ke mode awal *Roast Pedas*, pake perintah: `/mode_pedas`

Udah siap di\-roast? Kirim copywriting lo sekarang\!"""
        )
    
    async def handle_myaccount(self, update: telegram.Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handler for the /info_akun command."""
        user = update.effective_user
        account_data = self.db.get_user_data(user.id)
        
        if account_data:
            username = account_data["username"] or user.username or "User"
            usage_count = account_data["usage_count"]
            image_usage_count = account_data["image_usage_count"]
            
            myaccount_text = f"""
👤 *Hi, {username}* 👤

📊 *Statistik Penggunaan Bot* 📊
- Roast Teks Copywriting: *{usage_count} kali*
- Roast Gambar Copywriting: *{image_usage_count} kali*

🔥 Semangat jadi korban roasting! 🔥
            """
            await update.message.reply_markdown(myaccount_text)
        else:
            await update.message.reply_text(
                "Waduh, data akun kamu nggak ketemu di database! 😫 Coba /start dulu ya, atau mungkin ada error di database."
            )
    
    async def handle_mode_pedas(self, update: telegram.Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handler for the /mode_pedas command."""
        self.config.bot_mode = BotMode.SPICY
        await update.message.reply_html(
            "Oke! Mode bot sekarang di <strong>Roast Pedas</strong> 🔥 siap nyinyir abis-abisan! Kirimin copywriting lo, siap-siap di-roast tanpa ampun! 😂"
        )
    
    async def handle_mode_solusi(self, update: telegram.Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handler for the /mode_solusi command."""
        self.config.bot_mode = BotMode.SOLUTION
        await update.message.reply_html(
            "Sip! Mode bot ganti ke <strong>Roast Berfaedah</strong> 👍. Gue bakal tetep roast copywriting lo, tapi gue kasih juga masukan yang <strong>berfaedah</strong> dikit. Kirim copywriting lo, mari kita bedah! 😎"
        )
    
    async def handle_about(self, update: telegram.Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handler for the /tentang command."""
        about_text = """
Hai gaes! 👋 Gue adalah bot Telegram yang siap ngeroast copywriting lo sampe gosong! 🔥

Bot ini gue bikin buat hiburan semata ya, jangan baper kalo roast-nya kepedesan!

Nih kreator-nya, @navrex0 🔥

Kalo lo suka sama roast-roast yang pedas ini, dan pengen gue terus semangat ngembangin bot ini, boleh banget nih kasih dukungan ke link Trakteer gue di bawah ini 👇👇

<a href="https://trakteer.id/ervankurniawan41/tip">https://trakteer.id/ervankurniawan41/tip</a>

Makasih banyak ya buat supportnya! 🙏 Semoga skill copywriting lo makin mantep setelah di-roast sama gue dan rejeki lo lancar! 🔥🔥🔥
        """
        await update.message.reply_html(about_text, disable_web_page_preview=True)
    
    # Message handlers
    async def handle_text(self, update: telegram.Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handler for text messages."""
        user_copywriting = update.message.text
        
        if not user_copywriting:
            await update.message.reply_text("Eh, kirimin dulu dong teks copywriting yang mau di-roast!")
            return
        
        # Send initial message
        await context.bot.send_chat_action(chat_id=update.message.chat_id, action=ChatAction.TYPING)
        initial_message = await update.message.reply_text("Copywriting lo udah gue terima nih! jangan kabur lo!")
        
        # Get prompt based on current mode
        current_mode = self.config.bot_mode
        prompt = self.prompts[current_mode].format(text=user_copywriting)
        
        # Process with retry mechanism
        response_text = await self._process_with_retry(
            update, 
            context, 
            initial_message,
            self._generate_text_response,
            prompt,
            self.fallback_text
        )
        
        if response_text:
            # Delete initial message and send response
            await context.bot.delete_message(
                chat_id=update.message.chat_id,
                message_id=initial_message.message_id
            )
            
            # Increment usage count
            self.db.increment_usage_count(update.effective_user.id)
            
            # Send response
            await update.message.reply_text(response_text)
    
    async def handle_image(self, update: telegram.Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handler for image messages."""
        user = update.effective_user
        photo = update.message.photo[-1]
        file_id = photo.file_id
        
        # Send initial message
        await context.bot.send_chat_action(chat_id=update.message.chat_id, action=ChatAction.TYPING)
        initial_message = await update.message.reply_text("Gambar copywriting lo udah gue terima nih! Bentar ya, lagi gue bedah... 🧐")
        
        # Download image
        file = await context.bot.get_file(file_id)
        image_path = DOWNLOADS_DIR / f"{file_id}.jpg"
        await file.download_to_drive(image_path)
        
        try:
            # Process image with retry mechanism
            response_text = await self._process_with_retry(
                update,
                context,
                initial_message,
                self._generate_image_response,
                str(image_path),
                self.fallback_image
            )
            
            if response_text:
                # Delete initial message
                await context.bot.delete_message(
                    chat_id=update.message.chat_id,
                    message_id=initial_message.message_id
                )
                
                # Increment usage counts
                self.db.increment_usage_count(user.id)
                self.db.increment_image_usage_count(user.id)
                
                # Send response
                await update.message.reply_text(response_text)
        
        finally:
            # Clean up the downloaded image
            try:
                if image_path.exists():
                    image_path.unlink()
                    logger.info(f"Image file {image_path} successfully deleted.")
            except Exception as e:
                logger.error(f"Error deleting image file {image_path}: {e}")
    
    # Helper methods
    async def _process_with_retry(
        self, 
        update: telegram.Update, 
        context: ContextTypes.DEFAULT_TYPE,
        initial_message: telegram.Message,
        process_func: callable,
        process_input: str,
        fallback_message: str
    ) -> str:
        """Process a request with retry mechanism."""
        max_retries = 3
        retry_delay = 2
        
        for retry_count in range(1, max_retries + 1):
            logger.info(f"Attempting API call (attempt {retry_count})... (Mode: {self.config.bot_mode})")
            
            try:
                # Show typing indicator
                await context.bot.send_chat_action(chat_id=update.message.chat_id, action=ChatAction.TYPING)
                
                # Update status message
                await context.bot.edit_message_text(
                    chat_id=update.message.chat_id,
                    message_id=initial_message.message_id,
                    text=f"Wait, bahan lo lagi digoreng master chef pake mode *{self.config.bot_mode}*! 🔥",
                    parse_mode=ParseMode.MARKDOWN
                )
                
                # Process the request
                start_time = time.time()
                await context.bot.send_chat_action(chat_id=update.message.chat_id, action=ChatAction.TYPING)
                
                response = await process_func(process_input)
                
                end_time = time.time()
                logger.info(f"API call time: {end_time - start_time:.2f} seconds (Mode: {self.config.bot_mode})")
                
                if response:
                    return response
                
                # If we got an empty response but no exception, return fallback
                await update.message.reply_text(
                    "Hmm, API speechless... copywriting lo terlalu bagus (atau terlalu parah?)! Coba kirim yang lain deh."
                )
                return ""
                
            except Exception as e:
                logger.error(f"Error communicating with API: {e} (Mode: {self.config.bot_mode})")
                
                if retry_count < max_retries:
                    # Update status message for retry
                    await context.bot.edit_message_text(
                        chat_id=update.message.chat_id,
                        message_id=initial_message.message_id,
                        text=f"Waduh, mesin roasting mode *{self.config.bot_mode}* kayaknya lagi ngambek dikit... 😪\nGue coba sekali lagi ya... (percobaan ke-{retry_count + 1})"
                    )
                    time.sleep(retry_delay)
                else:
                    # All retries failed, update status message
                    await context.bot.edit_message_text(
                        chat_id=update.message.chat_id,
                        message_id=initial_message.message_id,
                        text=f"Waduh, mesin roasting mode *{self.config.bot_mode}* lagi ngambek! 😭 Sabar ya, lagi diperbaiki nih...",
                        parse_mode=ParseMode.MARKDOWN_V2
                    )
                    return fallback_message
        
        return fallback_message
    
    async def _generate_text_response(self, prompt: str) -> str:
        """Generate text response from the API."""
        response = self.config.text_model.generate_content(prompt)
        return response.text if response else ""
    
    async def _generate_image_response(self, image_path: str) -> str:
        """Generate response for an image from the API."""
        try:
            img = Image.open(image_path)
            response = self.config.vision_model.generate_content([self.image_prompt, img])
            return response.text if response else ""
        except Exception as e:
            logger.error(f"Error processing image: {e}")
            raise

async def error_handler(update: telegram.Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log errors caused by updates."""
    logger.error(f"Update {update} caused error {context.error}")

def main() -> None:
    """Initialize and start the bot."""
    try:
        # Create configuration
        config = Config()
        
        # Initialize database manager
        db_manager = DatabaseManager(DATABASE_FILE)
        
        # Create bot instance
        bot = RoastBot(config, db_manager)
        
        # Initialize the Application
        application = Application.builder().token(config.telegram_token).build()
        
        # Add command handlers
        application.add_handler(CommandHandler("start", bot.handle_start))
        application.add_handler(CommandHandler("mode_pedas", bot.handle_mode_pedas))
        application.add_handler(CommandHandler("mode_solusi", bot.handle_mode_solusi))
        application.add_handler(CommandHandler("tentang", bot.handle_about))
        application.add_handler(CommandHandler("info_akun", bot.handle_myaccount))
        
        # Add message handlers
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, bot.handle_text))
        application.add_handler(MessageHandler(filters.PHOTO, bot.handle_image))
        
        # Add error handler
        application.add_error_handler(error_handler)
        
        # Start the Bot
        logger.info("Starting bot...")
        application.run_polling(allowed_updates=telegram.Update.ALL_TYPES)
        
    except ValueError as e:
        logger.critical(f"Failed to start bot: {e}")
    except Exception as e:
        logger.critical(f"Unexpected error: {e}")

if __name__ == "__main__":
    main()