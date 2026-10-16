#!/usr/bin/env python3
"""
pdf_to_audio.py — Convierte un PDF de apuntes en un audiolibro narrado por un "profesor".

Usa Google Gemini (nivel gratuito) para generar la narración y edge-tts o Gemini TTS (gratuitos) para la voz.

Uso:
    python pdf_to_audio.py tema1.pdf
    python pdf_to_audio.py tema1.pdf --voz elvira --output apuntes_SO.mp3
    python pdf_to_audio.py tema1.pdf --solo-texto   # Solo genera el guion, sin audio
    python pdf_to_audio.py tema1.pdf --desde-texto  # Reutiliza el guion ya generado y solo crea el audio
    python pdf_to_audio.py tema1.pdf --tts gemini   # Usa la voz de Gemini en lugar de edge-tts

Configuración:
    Copia .env.example a .env y pon tu clave de Gemini (https://aistudio.google.com/apikey).
"""

import argparse
import asyncio
import os
import sys
import textwrap
import time
import wave
from pathlib import Path

# ─── Dependencias externas ───────────────────────────────────────────────────
try:
    from google import genai
    from google.genai import errors, types
except ImportError:
    sys.exit("❌ Instala las dependencias: pip install -r requirements.txt")

try:
    import pymupdf
except ImportError:
    sys.exit("❌ Instala las dependencias: pip install -r requirements.txt")

try:
    import edge_tts
except ImportError:
    sys.exit("❌ Instala las dependencias: pip install -r requirements.txt")

try:
    from dotenv import load_dotenv
    load_dotenv()  # Carga las variables del archivo .env, si existe
except ImportError:
    pass  # python-dotenv es opcional: también se pueden usar variables de entorno normales


# ─── Configuración ───────────────────────────────────────────────────────────

MODELO_POR_DEFECTO = os.environ.get("GEMINI_MODEL", "gemini-3-flash-preview")

VOCES_DISPONIBLES = {
    "alvaro": "es-ES-AlvaroNeural",   # Hombre, España
    "elvira": "es-ES-ElviraNeural",   # Mujer, España
    "jorge":  "es-MX-JorgeNeural",    # Hombre, México
    "dalia":  "es-MX-DaliaNeural",    # Mujer, México
}

SYSTEM_PROMPT = """Eres un profesor universitario experto y apasionado por su materia.
Tu tarea es transformar el contenido de unos apuntes o tema académico en una narración oral,
como si lo estuvieras explicando en clase a tus alumnos.

REGLAS ESTRICTAS:
1. El resultado debe sonar natural al escucharse en voz alta. Nada de listas con guiones,
   bullets, asteriscos ni formato Markdown. Solo prosa fluida.
2. Cuando encuentres una tabla, explica verbalmente lo que muestra, sus patrones y conclusiones.
   Ejemplo: "Si miramos los datos de la tabla, vemos que a medida que aumenta X, Y decrece de forma casi lineal..."
3. Cuando encuentres una imagen, diagrama o figura, descríbela e interpreta su significado.
   Ejemplo: "El diagrama nos muestra el flujo entre los tres componentes principales: primero..."
4. Cuando encuentres fórmulas o código, explícalos conceptualmente antes de detallarlos.
5. Usa transiciones naturales entre secciones: "Ahora bien...", "Pasando al siguiente punto...",
   "Esto nos lleva a preguntarnos...", "Relacionado con lo anterior..."
6. Añade contexto y ejemplos donde ayuden a la comprensión.
7. Evita frases como "como se puede ver en la figura" o "en la tabla anterior".
   Todo debe tener sentido sin ver nada.
8. El tono es didáctico, cercano y motivador, como un buen profesor que quiere que sus alumnos entiendan de verdad.
9. Empieza directamente con la narración, sin preámbulos como "Aquí tienes la narración..." o similares.
10. Termina con un breve resumen de los puntos clave del tema."""

MODELO_TTS = os.environ.get("GEMINI_TTS_MODEL", "gemini-2.5-flash-preview-tts")
VOZ_GEMINI_POR_DEFECTO = "Charon"   # Voces disponibles: https://ai.google.dev/gemini-api/docs/speech-generation
MAX_CARACTERES_TTS = 3000           # Gemini TTS genera el audio por fragmentos de este tamaño

MAX_PAGINAS_POR_CHUNK = 20
MAX_TOKENS_SALIDA = 16000   # Margen amplio para que la narración de 20 páginas no se corte
REINTENTOS = 3              # Reintentos ante errores temporales (límite de peticiones, servidor saturado)


# ─── Procesamiento del PDF ───────────────────────────────────────────────────

def extraer_paginas_como_imagenes(pdf_path: Path, dpi: int = 150) -> list[dict]:
    """
    Extrae cada página como imagen PNG.
    Usar imágenes garantiza que el modelo vea tablas y diagramas correctamente.
    """
    paginas = []
    with pymupdf.open(pdf_path) as doc:
        print(f"📄 PDF cargado: {len(doc)} páginas")
        matriz = pymupdf.Matrix(dpi / 72, dpi / 72)
        for i, page in enumerate(doc):
            pix = page.get_pixmap(matrix=matriz)
            paginas.append({"numero": i + 1, "png": pix.tobytes("png")})
    return paginas


def agrupar_en_chunks(paginas: list[dict], tamaño: int) -> list[list[dict]]:
    return [paginas[i:i + tamaño] for i in range(0, len(paginas), tamaño)]


# ─── Llamada a Gemini ────────────────────────────────────────────────────────

def contexto_del_chunk(chunk: list[dict], parte_num: int, total_chunks: int) -> str:
    """Indica al modelo en qué parte del documento está para que la narración sea continua."""
    if total_chunks == 1:
        return "A continuación tienes el documento completo."
    inicio, fin = chunk[0]["numero"], chunk[-1]["numero"]
    if parte_num == 1:
        return f"Páginas {inicio}–{fin} (parte 1 de {total_chunks}). Es el inicio del tema."
    if parte_num == total_chunks:
        return f"Páginas {inicio}–{fin} (parte final). Termina con el resumen de TODO el tema."
    return (f"Páginas {inicio}–{fin} (parte {parte_num} de {total_chunks}). "
            "Continúa la narración sin repetir lo anterior.")


def con_reintentos(llamada):
    """Ejecuta una llamada a Gemini reintentando ante errores temporales (límite de peticiones, servidor saturado)."""
    for intento in range(1, REINTENTOS + 1):
        try:
            return llamada()
        except (errors.ServerError, errors.ClientError) as e:
            temporal = isinstance(e, errors.ServerError) or getattr(e, "code", None) == 429
            if not temporal or intento == REINTENTOS:
                raise
            espera = 20 * intento
            print(f"\n  ⏳ Error temporal ({getattr(e, 'code', '?')}). Reintentando en {espera} s...", end="", flush=True)
            time.sleep(espera)


def llamar_gemini(client: genai.Client, modelo: str, chunk: list[dict], parte_num: int, total_chunks: int) -> str:
    """Llama a Gemini con un chunk de páginas del PDF, con reintentos ante errores temporales."""
    print(f"  🤖 Generando narración (parte {parte_num}/{total_chunks})...", end="", flush=True)

    partes = [contexto_del_chunk(chunk, parte_num, total_chunks)]
    partes += [types.Part.from_bytes(data=p["png"], mime_type="image/png") for p in chunk]
    partes.append("Genera la narración del profesor para este fragmento.")

    response = con_reintentos(lambda: client.models.generate_content(
        model=modelo,
        contents=partes,
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            max_output_tokens=MAX_TOKENS_SALIDA,
        ),
    ))

    texto = response.text
    if not texto:
        raise RuntimeError(f"Gemini no devolvió texto en la parte {parte_num}.")

    motivo = response.candidates[0].finish_reason if response.candidates else None
    if motivo == types.FinishReason.MAX_TOKENS:
        print(" ⚠️  (la respuesta se cortó por longitud; prueba con menos páginas por parte)", end="")

    print(f" ✓ ({len(texto)} caracteres)")
    return texto


def generar_narracion(client: genai.Client, pdf_path: Path, modelo: str) -> str:
    print("\n📖 Extrayendo páginas del PDF...")
    paginas = extraer_paginas_como_imagenes(pdf_path)

    chunks = agrupar_en_chunks(paginas, MAX_PAGINAS_POR_CHUNK)
    total = len(chunks)
    print(f"🔪 Dividido en {total} parte(s) de máximo {MAX_PAGINAS_POR_CHUNK} páginas\n")

    narraciones = [
        llamar_gemini(client, modelo, chunk, parte_num=i + 1, total_chunks=total)
        for i, chunk in enumerate(chunks)
    ]
    return "\n\n".join(narraciones)


# ─── Conversión a audio ──────────────────────────────────────────────────────

async def audio_edge_tts(texto: str, output_path: Path, voz: str):
    """Genera un MP3 con edge-tts (servicio de voz de Microsoft Edge)."""
    print(f"\n🎙️  Convirtiendo a audio con edge-tts (voz '{voz}')...")
    communicate = edge_tts.Communicate(texto, voz)
    await communicate.save(str(output_path))


def dividir_texto(texto: str, max_caracteres: int) -> list[str]:
    """Divide el texto en fragmentos por párrafos (o frases, si un párrafo es demasiado largo)."""
    fragmentos, actual = [], ""
    for parrafo in texto.split("\n"):
        piezas = [parrafo] if len(parrafo) <= max_caracteres else parrafo.replace(". ", ".\n").split("\n")
        for pieza in piezas:
            if len(actual) + len(pieza) + 1 > max_caracteres and actual:
                fragmentos.append(actual.strip())
                actual = ""
            actual += pieza + "\n"
    if actual.strip():
        fragmentos.append(actual.strip())
    return fragmentos


def audio_gemini_tts(client: genai.Client, texto: str, output_path: Path, voz: str):
    """Genera un WAV con la voz de Gemini, fragmento a fragmento."""
    fragmentos = dividir_texto(texto, MAX_CARACTERES_TTS)
    print(f"\n🎙️  Convirtiendo a audio con Gemini TTS (voz '{voz}', {len(fragmentos)} fragmento(s))...")
    config = types.GenerateContentConfig(
        response_modalities=["AUDIO"],
        speech_config=types.SpeechConfig(
            voice_config=types.VoiceConfig(prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voz))
        ),
    )
    pcm = bytearray()
    for i, fragmento in enumerate(fragmentos, start=1):
        print(f"  🔊 Fragmento {i}/{len(fragmentos)}...", end="", flush=True)
        response = con_reintentos(lambda: client.models.generate_content(
            model=MODELO_TTS, contents=f"Lee este texto en español, con tono de profesor cercano: {fragmento}", config=config))
        pcm += response.candidates[0].content.parts[0].inline_data.data
        print(" ✓")
    # Gemini devuelve audio PCM de 16 bits, mono, a 24 kHz
    with wave.open(str(output_path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(24000)
        wav.writeframes(bytes(pcm))


def mostrar_tamaño(output_path: Path):
    size_mb = output_path.stat().st_size / (1024 * 1024)
    print(f"✅ Audio guardado: {output_path} ({size_mb:.1f} MB)")


# ─── Pipeline principal ──────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Convierte un PDF de apuntes en un audiolibro explicado por un profesor.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""
            Ejemplos:
              python pdf_to_audio.py tema1.pdf
              python pdf_to_audio.py tema1.pdf --voz elvira
              python pdf_to_audio.py tema1.pdf --voz es-MX-JorgeNeural
              python pdf_to_audio.py tema1.pdf --solo-texto
              python pdf_to_audio.py tema1.pdf --output carpeta/mi_audio.mp3
              python pdf_to_audio.py tema1.pdf --desde-texto --tts gemini

            Voces predefinidas:
              alvaro  → es-ES-AlvaroNeural  (hombre, España)
              elvira  → es-ES-ElviraNeural  (mujer, España)
              jorge   → es-MX-JorgeNeural   (hombre, México)
              dalia   → es-MX-DaliaNeural   (mujer, México)
            Con --tts gemini, --voz es una voz de Gemini (por defecto, Charon).

            Clave gratuita de Gemini en: https://aistudio.google.com/apikey
        """),
    )
    parser.add_argument("pdf", help="Ruta al archivo PDF")
    parser.add_argument("--voz", default=None,
                        help="Voz para el audio (edge-tts: nombre corto o ID completo; Gemini: nombre de la voz)")
    parser.add_argument("--tts", choices=["edge", "gemini"], default="edge",
                        help="Motor de voz: edge (MP3, por defecto) o gemini (WAV). Si edge falla, se usa gemini")
    parser.add_argument("--output", default=None,
                        help="Ruta del archivo de audio de salida")
    parser.add_argument("--solo-texto", action="store_true",
                        help="Solo genera el guion de narración, sin convertirlo a audio")
    parser.add_argument("--desde-texto", action="store_true",
                        help="Reutiliza el guion ya generado (<pdf>_narracion.txt) y solo crea el audio")
    parser.add_argument("--modelo", default=MODELO_POR_DEFECTO,
                        help=f"Modelo de Gemini a usar (por defecto: {MODELO_POR_DEFECTO})")
    parser.add_argument("--api-key", default=None,
                        help="Clave de Gemini (alternativa a GEMINI_API_KEY en el .env)")
    args = parser.parse_args()

    pdf_path = Path(args.pdf)
    if not pdf_path.exists():
        sys.exit(f"❌ No se encuentra el archivo: {pdf_path}")
    if pdf_path.suffix.lower() != ".pdf":
        sys.exit("❌ El archivo debe ser un PDF")

    api_key = args.api_key or os.environ.get("GEMINI_API_KEY")
    if not api_key:
        sys.exit("❌ Necesitas una clave de Gemini. Consíguela gratis en https://aistudio.google.com/apikey\n"
                 "   y ponla en el archivo .env (GEMINI_API_KEY=...) o pásala con --api-key")

    client = genai.Client(api_key=api_key)

    txt_output = pdf_path.with_name(f"{pdf_path.stem}_narracion.txt")

    def ruta_audio(extension: str) -> Path:
        if args.output:
            return Path(args.output).with_suffix(extension)
        return pdf_path.with_name(f"{pdf_path.stem}_audio{extension}")

    voz_edge = VOCES_DISPONIBLES.get((args.voz or "alvaro").lower(), args.voz or "alvaro")
    voz_gemini = args.voz or VOZ_GEMINI_POR_DEFECTO
    voz_mostrada = voz_gemini if args.tts == "gemini" else voz_edge

    print(f"""
╔══════════════════════════════════════════╗
║        PDF → Audiolibro del Profesor     ║
╚══════════════════════════════════════════╝
  📄 PDF:    {pdf_path.name}
  🧠 Modelo: {args.modelo}
  🎙️  Voz:    {voz_mostrada} ({args.tts})
  📝 Guion:  {txt_output.name}{' (reutilizado)' if args.desde_texto else ''}
""")

    if args.desde_texto:
        if not txt_output.exists():
            sys.exit(f"❌ No existe el guion {txt_output.name}. Ejecuta primero sin --desde-texto.")
        narracion = txt_output.read_text(encoding="utf-8")
    else:
        try:
            narracion = generar_narracion(client, pdf_path, args.modelo)
        except Exception as e:
            sys.exit(f"\n❌ Error al generar la narración con Gemini: {e}")
        txt_output.write_text(narracion, encoding="utf-8")
        print(f"\n📝 Guion guardado: {txt_output}")

    if args.solo_texto:
        print("\n🎓 ¡Listo! Guion generado.")
        return

    usar_gemini = args.tts == "gemini"
    if not usar_gemini:
        audio_output = ruta_audio(".mp3")
        try:
            audio_output.parent.mkdir(parents=True, exist_ok=True)
            asyncio.run(audio_edge_tts(narracion, audio_output, voz_edge))
            mostrar_tamaño(audio_output)
        except Exception as e:
            print(f"\n⚠️  edge-tts ha fallado ({e}).")
            print("   Probando con la voz de Gemini...")
            audio_output.unlink(missing_ok=True)
            usar_gemini = True

    if usar_gemini:
        audio_output = ruta_audio(".wav")
        try:
            audio_output.parent.mkdir(parents=True, exist_ok=True)
            audio_gemini_tts(client, narracion, audio_output, voz_gemini)
            mostrar_tamaño(audio_output)
        except Exception as e:
            print(f"\n⚠️  Error al generar el audio con Gemini: {e}")
            print(f"   El guion de texto sí está guardado en: {txt_output}")
            print("   Puedes reintentar solo el audio con: --desde-texto")
            sys.exit(1)

    print("\n🎓 ¡Listo! Ya tienes tus apuntes en formato audiolibro.")


if __name__ == "__main__":
    main()
