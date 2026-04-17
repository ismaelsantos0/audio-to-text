import os
import asyncio
import subprocess
import logging
import time
import json
from datetime import datetime
from pathlib import Path
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    MessageHandler,
    CommandHandler,
    ContextTypes,
    filters,
)
import whisper
from pydub import AudioSegment
import tempfile
import shutil

# ============================================================================
# CONFIGURAÇÃO DE LOGGING
# ============================================================================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ============================================================================
# VARIÁVEIS GLOBAIS
# ============================================================================
TOKEN = os.getenv("BOT_TOKEN")
RAILWAY_ENV = os.getenv("RAILWAY_ENVIRONMENT_NAME", "local")
CHUNK_LENGTH_SECONDS = 10 * 60  # 10 minutos de áudio por chunk (otimizado)
MODEL_SIZE = os.getenv("WHISPER_MODEL", "base")  # "tiny" para mais rápido

if not TOKEN:
    raise ValueError("❌ BOT_TOKEN não definida. Configure no Railway.")

logger.info(f"🚀 Ambiente: {RAILWAY_ENV}")
logger.info(f"📊 Modelo Whisper: {MODEL_SIZE}")
logger.info(f"🔪 Chunk: {CHUNK_LENGTH_SECONDS // 60} minutos")

# ============================================================================
# CARREGAMENTO DE MODELO (feito uma vez)
# ============================================================================
logger.info("⏳ Carregando modelo Whisper...")
try:
    model = whisper.load_model(MODEL_SIZE)
    logger.info("✅ Modelo carregado com sucesso")
except Exception as e:
    logger.error(f"❌ Erro ao carregar modelo: {e}")
    raise

# ============================================================================
# GERENCIAMENTO DE DIRETÓRIOS
# ============================================================================
TEMP_DIR = Path(tempfile.gettempdir()) / "telegram_transcriber"
TEMP_DIR.mkdir(exist_ok=True)

def cleanup_temp_files():
    """Remove arquivos temporários antigos"""
    try:
        for file in TEMP_DIR.glob("*"):
            if time.time() - file.stat().st_mtime > 3600:  # Mais de 1h
                file.unlink()
                logger.debug(f"Limpou: {file}")
    except Exception as e:
        logger.warning(f"Erro ao limpar temp: {e}")

# ============================================================================
# HANDLERS
# ============================================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando /start"""
    await update.message.reply_text(
        "🎙️ **Bot de Transcrição de Áudio**\n\n"
        "Envie um áudio para transcrever em texto.\n\n"
        "⏱️ Suporta áudios até 1 hora\n"
        "📝 Idioma: Português (BR)\n"
        "⚡ Rodando em Railway\n\n"
        "Processamento pode levar alguns minutos...",
        parse_mode="Markdown"
    )

async def handle_audio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Processa áudios grandes (até 1 hora)
    - Converte para WAV
    - Divide em chunks
    - Transcreve com Whisper
    - Retorna texto completo
    """
    message = update.message
    user_id = message.from_user.id
    message_id = message.message_id
    
    # ID único para esta requisição
    request_id = f"{user_id}_{message_id}_{int(time.time())}"
    work_dir = TEMP_DIR / request_id
    work_dir.mkdir(exist_ok=True)
    
    logger.info(f"📥 [ID:{request_id}] Áudio recebido de {user_id}")
    
    try:
        # ====================================================================
        # STEP 1: IDENTIFICAR E BAIXAR
        # ====================================================================
        file = None
        if message.voice:
            file = await message.voice.get_file()
            duration = message.voice.duration
        elif message.audio:
            file = await message.audio.get_file()
            duration = message.audio.duration
        else:
            await message.reply_text("❌ Envie um áudio ou mensagem de voz válida.")
            return
        
        # Validar tamanho (Telegram limit é 50MB)
        file_size_mb = file.file_size / (1024 * 1024)
        logger.info(f"📊 [ID:{request_id}] Tamanho: {file_size_mb:.1f}MB, Duração: {duration}s ({duration//60}m)")
        
        if duration and duration > 3600:  # Mais de 1h
            await message.reply_text(
                "⚠️ Áudio muito longo (máximo 1 hora).\n"
                f"Seu áudio: {duration//60} minutos"
            )
            return
        
        status_msg = await message.reply_text("📥 Baixando áudio...")
        input_path = work_dir / "input.ogg"
        
        await file.download_to_drive(str(input_path))
        logger.info(f"✅ [ID:{request_id}] Arquivo baixado: {input_path.stat().st_size / 1024 / 1024:.1f}MB")
        
        # ====================================================================
        # STEP 2: CONVERTER PARA WAV
        # ====================================================================
        await status_msg.edit_text("🔄 Convertendo áudio...")
        wav_path = work_dir / "audio.wav"
        
        result = subprocess.run(
            [
                "ffmpeg",
                "-i", str(input_path),
                "-acodec", "pcm_s16le",
                "-ar", "16000",  # Whisper recomenda 16kHz
                "-ac", "1",       # Mono
                "-q:a", "9",
                "-n",
                str(wav_path)
            ],
            capture_output=True,
            text=True,
            timeout=300  # 5 minutos max
        )
        
        if result.returncode != 0:
            logger.error(f"❌ [ID:{request_id}] FFmpeg error: {result.stderr}")
            await status_msg.edit_text("❌ Erro ao converter áudio. Arquivo pode estar corrompido.")
            return
        
        wav_size_mb = wav_path.stat().st_size / 1024 / 1024
        logger.info(f"✅ [ID:{request_id}] WAV convertido: {wav_size_mb:.1f}MB")
        
        # ====================================================================
        # STEP 3: DIVIDIR EM CHUNKS
        # ====================================================================
        await status_msg.edit_text("✂️ Dividindo áudio em partes...")
        
        audio = AudioSegment.from_wav(str(wav_path))
        chunk_length_ms = CHUNK_LENGTH_SECONDS * 1000
        chunks = []
        
        total_chunks = (len(audio) // chunk_length_ms) + (1 if len(audio) % chunk_length_ms else 0)
        logger.info(f"📊 [ID:{request_id}] {total_chunks} chunks de {CHUNK_LENGTH_SECONDS}s cada")
        
        for i in range(0, len(audio), chunk_length_ms):
            chunk = audio[i:i + chunk_length_ms]
            chunk_path = work_dir / f"chunk_{i//chunk_length_ms:03d}.wav"
            chunk.export(str(chunk_path), format="wav")
            chunks.append(chunk_path)
        
        logger.info(f"✅ [ID:{request_id}] {len(chunks)} chunks criados")
        
        # ====================================================================
        # STEP 4: TRANSCREVER
        # ====================================================================
        full_text = ""
        transcription_start = time.time()
        
        for i, chunk_path in enumerate(chunks):
            try:
                # Atualizar status
                progress_pct = int((i / len(chunks)) * 100)
                await status_msg.edit_text(
                    f"🧠 Transcrevendo...\n"
                    f"Parte {i+1}/{len(chunks)} ({progress_pct}%)"
                )
                logger.info(f"⏳ [ID:{request_id}] Transcrevendo chunk {i+1}/{len(chunks)}")
                
                # Transcrever
                chunk_start = time.time()
                result = model.transcribe(
                    str(chunk_path),
                    language="pt",
                    verbose=False,
                    fp16=False  # Desabilitar FP16 se der problema
                )
                chunk_time = time.time() - chunk_start
                
                text_chunk = result["text"].strip()
                if text_chunk:
                    full_text += text_chunk + "\n"
                
                logger.info(f"✅ [ID:{request_id}] Chunk {i+1} transcrito em {chunk_time:.1f}s")
                
            except Exception as e:
                logger.error(f"❌ [ID:{request_id}] Erro no chunk {i+1}: {str(e)}")
                await message.reply_text(f"⚠️ Erro ao transcrever parte {i+1}")
        
        transcription_time = time.time() - transcription_start
        logger.info(f"✅ [ID:{request_id}] Transcrição completa em {transcription_time:.1f}s ({transcription_time//60}m)")
        
        # ====================================================================
        # STEP 5: ENVIAR RESULTADO
        # ====================================================================
        if not full_text.strip():
            await status_msg.edit_text(
                "⚠️ Nenhuma transcrição detectada.\n"
                "O áudio pode estar muito silencioso ou ilegível."
            )
            logger.warning(f"❌ [ID:{request_id}] Nenhum texto detectado")
            return
        
        # Stats
        stats = (
            f"📊 **Estatísticas:**\n"
            f"⏱️ Duração: {duration//60}m {duration%60}s\n"
            f"⚡ Processado em: {transcription_time/60:.1f}m\n"
            f"📝 Caracteres: {len(full_text)}\n"
            f"📄 Parágrafos: {len(full_text.split(chr(10)))}\n\n"
        )
        
        await status_msg.edit_text(stats + "✅ **Transcrição Completa!**")
        
        # Enviar texto (dividido em chunks de 4096 caracteres)
        max_chars = 4000
        if len(full_text) > max_chars:
            chunks_text = [
                full_text[i:i+max_chars] 
                for i in range(0, len(full_text), max_chars)
            ]
            
            await message.reply_text(
                f"📄 Dividido em {len(chunks_text)} mensagens:"
            )
            
            for j, chunk_text in enumerate(chunks_text, 1):
                await message.reply_text(
                    f"**[Parte {j}/{len(chunks_text)}]**\n\n{chunk_text}"
                )
        else:
            await message.reply_text(f"**[Transcrição]**\n\n{full_text}")
        
        logger.info(f"✅ [ID:{request_id}] Resultado enviado ao usuário")
        
    except asyncio.TimeoutError:
        logger.error(f"❌ [ID:{request_id}] Timeout")
        await message.reply_text("⏱️ Tempo de processamento excedido. Tente um áudio menor.")
    
    except Exception as e:
        logger.error(f"❌ [ID:{request_id}] Erro geral: {str(e)}", exc_info=True)
        await message.reply_text(f"❌ Erro ao processar: {str(e)[:100]}")
    
    finally:
        # Limpeza
        try:
            shutil.rmtree(work_dir, ignore_errors=True)
            logger.debug(f"🗑️ [ID:{request_id}] Diretório de trabalho removido")
        except Exception as e:
            logger.warning(f"⚠️ [ID:{request_id}] Erro ao limpar: {e}")

async def main():
    """Inicia a aplicação"""
    
    # Limpeza inicial
    cleanup_temp_files()
    
    # Build app
    app = ApplicationBuilder().token(TOKEN).build()
    
    # Handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_audio))
    
    logger.info("=" * 60)
    logger.info("🚀 BOT INICIADO - Pronto para processar áudios")
    logger.info("=" * 60)
    
    # Polling
    await app.run_polling()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("⏹️ Bot encerrado pelo usuário")
    except Exception as e:
        logger.error(f"❌ Erro fatal: {e}", exc_info=True)
