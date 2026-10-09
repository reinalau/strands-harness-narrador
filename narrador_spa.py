"""
mini-Sherpa — Narrador de historias con Strands Agents Harness.

Todo sigue patrones DOCUMENTADOS del Strands harness / Strands Harness SDK:

- Subagentes = "Agents as Tools" (doc: Delegate to subagents). continuidad y estructura
  son `strands.Agent` crudos con name/description/system_prompt, pasados via `.as_tool()`.
  Ref: https://strandsagents.com/docs/user-guide/harness/configure/subagents/
- El bibliotecario usa STRUCTURED OUTPUT (doc: Return structured output): un agente Nova
  con `structured_output_model=Biblia` (Pydantic), expuesto como tool `@tool`. Devuelve
  un JSON validado en vez de texto libre.
  Ref: https://strandsagents.com/docs/user-guide/sdk/agents/structured-output/
- Human-in-the-loop = chat por turnos (NO un gate de aprobacion). El agente escribe un
  capitulo, lo guarda y TERMINA su turno; no encadena el siguiente. El control vuelve a
  la persona, que lee/edita los archivos y en su proximo mensaje pide "revisar" o
  "continuar". El agente relee desde disco antes de actuar. Es el flujo natural del
  loop de chat; no requiere `interventions`.
- Los modelos se instancian con GeminiModel / BedrockModel (doc: strands.models.*). Un
  Agent crudo necesita una instancia de Model; los strings "google/..." / "bedrock/..."
  SOLO los resuelve create_harness().
  Ref: https://strandsagents.com/docs/api/python/strands.models.gemini/

DOS MODELOS:
  - Orquestador (escribe la prosa y en disco): Gemini 3.5 Flash.
  - Subagentes (solo analisis, no tocan disco): Nova Lite en Bedrock, mas barato.
"""

import os
import sys

# Windows: la consola usa cp1252 y crashea al imprimir caracteres no-ascii (acentos,
# emojis). Forzamos UTF-8 para que la salida no tumbe el proceso.
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# Trabajar siempre desde la carpeta de este script, para que las rutas relativas
# (story/, bible.json, state.json) resuelvan aqui.
os.chdir(os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv

load_dotenv()  # carga GEMINI_API_KEY

from typing import Optional

from pydantic import BaseModel, Field
from strands import Agent, tool
from strands.models.bedrock import BedrockModel
from strands_harness import create_harness

# DOS MODELOS:
#  - Orquestador (escribe la prosa y en disco): Gemini 3.5 Flash. Maneja bien las rutas
#    de Windows (a diferencia de Nova) y tiene cuota gratuita propia.
#  - Subagentes (solo analisis, no tocan disco): Nova Lite en Bedrock, mas barato.
GEMINI_MODEL_ID = "gemini-3.5-flash"
EFFORT = "off"

NOVA_MODEL_ID = "us.amazon.nova-lite-v1:0"


def nova_model():
    """Instancia de modelo Nova Lite (Bedrock) para los subagentes de solo analisis.

    Doc: strands.models.bedrock. El Agent crudo necesita una instancia de Model; el
    string "bedrock/..." solo lo entiende create_harness(). region_name toma AWS_REGION
    o us-west-2 por defecto; aqui lo fijamos a la region configurada (us-east-1).
    """
    return BedrockModel(
        model_id=NOVA_MODEL_ID,
        region_name=os.environ.get("AWS_REGION", "us-east-1"),
    )


# ---------------------------------------------------------------------------
# Schema de la biblia (structured output del bibliotecario).
#
# Doc: Return structured output. Se pasa un modelo Pydantic como structured_output_model
# y se lee result.structured_output ya validado. Para el bibliotecario esto reemplaza el
# "devolve JSON sin ```" fragil por un objeto garantizado-valido.
# Ref: https://strandsagents.com/docs/user-guide/sdk/agents/structured-output/
# ---------------------------------------------------------------------------


class Meta(BaseModel):
    titulo: str = Field(description="Titulo de la historia")
    genero: str = Field(description="Genero")
    tono: str = Field(description="Tono")
    punto_de_vista: str = Field(description="Punto de vista narrativo")
    ultimo_capitulo: int = Field(description="Numero del ultimo capitulo cerrado")


class Personaje(BaseModel):
    nombre: str
    rol: str
    objetivo: str
    desde_capitulo: int = Field(description="Capitulo en que aparece por primera vez")


class Relacion(BaseModel):
    a: str = Field(description="Un personaje")
    b: str = Field(description="El otro personaje")
    tipo: str = Field(description="Tipo de relacion")
    desde_capitulo: int


class QuienSabeQue(BaseModel):
    personaje: str
    sabe: str = Field(description="Que sabe el personaje")
    desde_capitulo: int


class HiloAbierto(BaseModel):
    descripcion: str
    abierto_en_capitulo: int
    cerrado_en_capitulo: Optional[int] = Field(
        default=None, description="Capitulo en que se cierra, o null si sigue abierto"
    )


class Objeto(BaseModel):
    nombre: str
    descripcion: str
    desde_capitulo: int


class EntradaLineaTiempo(BaseModel):
    capitulo: int
    eventos: list[str]


class Biblia(BaseModel):
    """Biblia de la historia. Es el structured_output del bibliotecario."""

    meta: Meta
    personajes: list[Personaje]
    relaciones: list[Relacion]
    quien_sabe_que: list[QuienSabeQue]
    hilos_abiertos: list[HiloAbierto]
    objetos: list[Objeto]
    linea_de_tiempo: list[EntradaLineaTiempo]


# ---------------------------------------------------------------------------
# Subagentes = Agents as Tools (patron documentado). Agent crudo + as_tool().
# Son de SOLO analisis: no tienen herramientas de disco; reciben texto y devuelven
# texto. El orquestador es el unico que persiste.
# ---------------------------------------------------------------------------

continuidad = Agent(
    name="continuidad",
    description=(
        "Revisa la continuidad de un capitulo contra la biblia de la historia. "
        "Pasale el contenido de bible.json y el texto del capitulo. Devuelve un "
        "reporte de contradicciones, conocimiento imposible e hilos perdidos."
    ),
    model=nova_model(),
    system_prompt=(
        "Sos un revisor de continuidad narrativa en espanol. Recibis la biblia de la "
        "historia (JSON) y el texto de un capitulo. Reporta SOLO problemas, de forma "
        "concreta y accionable: (1) contradicciones con la biblia (personajes, "
        "relaciones, objetos, linea de tiempo); (2) conocimiento imposible (un "
        "personaje sabe algo que no deberia saber todavia); (3) hilos abiertos que se "
        "abandonan. No reescribas el capitulo; solo lista los problemas. Si no hay "
        "problemas, deci 'Sin problemas de continuidad'."
    ),
)

estructura = Agent(
    name="estructura",
    description=(
        "Audita la estructura y el ritmo de un cuento o capitulo. Pasale el modo "
        "(cuento/novela), la posicion del capitulo si aplica, y el texto. Devuelve un "
        "reporte sobre etapas narrativas y ritmo."
    ),
    model=nova_model(),
    system_prompt=(
        "Sos un editor de estructura y ritmo narrativo en espanol. Audita el texto y "
        "reporta SOLO lo que falla o se puede mejorar.\n"
        "CUENTO: verifica inicio, incidente desencadenante, nudo con tension en alza, "
        "climax claro y desenlace que cambie el equilibrio inicial.\n"
        "CAPITULO DE NOVELA: verifica objetivo inicial del personaje, nudo (problema o "
        "revelacion) y final en GANCHO/cliffhanger (no cierre). Considera su posicion "
        "en los tres actos (planteamiento/confrontacion/resolucion).\n"
        "RITMO (siempre): mostrar en vez de contar; dramatizar los momentos clave y "
        "resumir el paso del tiempo; entrar tarde y salir temprano de cada escena.\n"
        "No reescribas el texto; lista observaciones concretas. Si esta bien, deci "
        "'Estructura y ritmo correctos'."
    ),
)

# El bibliotecario usa STRUCTURED OUTPUT (doc: Return structured output). En vez de
# devolver "JSON sin ```" y rezar, el agente Nova produce un objeto Biblia validado por
# Pydantic. Se expone al orquestador como una tool @tool: recibe la biblia actual y el
# capitulo, y devuelve el JSON validado (string) que el orquestador escribe en disco.
_bibliotecario_agent = Agent(
    model=nova_model(),
    system_prompt=(
        "Sos el bibliotecario de la historia en espanol. Recibis la biblia actual "
        "(JSON, puede venir vacia) y el texto de un capitulo. Actualiza la biblia: "
        "conserva lo que ya habia y agrega lo nuevo del capitulo. Las relaciones son "
        "una lista de aristas. Marca 'desde_capitulo' donde corresponda."
    ),
)


@tool
def bibliotecario(biblia_actual: str, texto_capitulo: str, numero_capitulo: int) -> str:
    """Actualiza la biblia de la historia con un capitulo nuevo y devuelve el JSON validado.

    Args:
        biblia_actual: Contenido actual de bible.json (o "" / "{}" si es el capitulo 1).
        texto_capitulo: Texto completo del capitulo recien escrito.
        numero_capitulo: Numero del capitulo (1, 2, ...).

    Returns:
        El JSON de la biblia actualizada, validado contra el esquema Biblia.
    """
    prompt = (
        f"Biblia actual:\n{biblia_actual or '(vacia, es el primer capitulo)'}\n\n"
        f"Capitulo {numero_capitulo}:\n{texto_capitulo}\n\n"
        "Devolve la biblia completa y actualizada."
    )
    result = _bibliotecario_agent(prompt, structured_output_model=Biblia)
    biblia: Biblia = result.structured_output
    return biblia.model_dump_json(indent=2)

# ---------------------------------------------------------------------------
# Prompt del orquestador (narrador)
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """\
Sos un narrador de historias en espanol y el ORQUESTADOR de un conjunto de agentes.
Tu salida es SOLO texto en espanol. No uses shell ni web. Tus herramientas son de
archivo (read, write, edit) y tres subagentes que podes invocar: "continuidad",
"estructura" y "bibliotecario". SOLO VOS escribis en disco; los subagentes solo
analizan y te devuelven texto.

ENTREVISTA INICIAL
Si falta formato (cuento o novela), genero, tono, personajes y premisa, pregunta todo
en UN SOLO mensaje breve y numerado. Si el usuario no decide algo, propone un valor y
avanza.

ESTRUCTURA NARRATIVA (seguila al escribir)
Cuento: inicio (puede ser in media res) -> incidente desencadenante -> nudo (tension en
alza) -> climax -> desenlace que cambia el equilibrio inicial. Ritmo rapido y directo.
Novela (macro): Acto 1 planteamiento (~20-25%, termina en punto de giro) -> Acto 2
confrontacion (~50%, con punto medio de no retorno) -> Acto 3 resolucion (~20-25%,
climax y cierre). Cada capitulo (micro) es un mini-cuento: objetivo inicial -> nudo
(problema o revelacion) -> final en GANCHO/cliffhanger, no en cierre.
Siempre: mostra en vez de explicar; dramatiza los momentos clave y resumi el paso del
tiempo; entra tarde y sali temprano de cada escena.

MODO CUENTO
Un capitulo de 1200 a 2500 palabras. Entrega el texto en el chat. Si la persona quiere
editarlo y pedir revision, guardalo en story/cuento.md.

MODO NOVELA - para cada capitulo, en este orden:
1. Si existe bible.json, LEELA desde disco antes de escribir. Si no existe, es el
   capitulo 1.
2. Escribi el capitulo (800 a 1500 palabras) respetando la biblia y la estructura.
3. DELEGA en "continuidad" (biblia + capitulo nuevo): que reporte contradicciones,
   conocimiento imposible e hilos perdidos.
4. DELEGA en "estructura": que audite etapas y ritmo del capitulo.
5. Aplica las correcciones que correspondan (vos decidis; los subagentes solo reportan).
6. Guarda el capitulo en story/capitulo-NN.md.
7. DELEGA en "bibliotecario": que te DEVUELVA el bible.json actualizado. Escribi vos ese
   JSON en bible.json.
8. Actualiza state.json (ultimo_capitulo, y el capitulo con n, titulo, archivo, estado
   "generado", cliffhanger).

HUMAN-IN-THE-LOOP (un capitulo por turno; parar y esperar)
Escribi UN SOLO capitulo (o el cuento) por turno. Despues de guardarlo, PARA: avisa a
la persona que puede leer y editar a mano los archivos (el .md y/o bible.json), y que
cuando quiera te pida "revisar" o "continuar". NUNCA empieces el siguiente capitulo por
tu cuenta; espera a que la persona te lo pida explicitamente.
Cuando te pidan REVISAR: RELEE los archivos desde disco (no asumas que tu contexto es lo
ultimo, la persona pudo editarlos), delega en "continuidad" y "estructura" sobre lo que
haya en disco, reporta, y actualiza el estado del capitulo a "revisado" en state.json.
Cuando te pidan CONTINUAR: RELEE bible.json y el ultimo capitulo desde disco antes de
escribir el siguiente.

INFORME DE CAPITULOS
Si la persona lo pide, lee state.json y devolve una enumeracion con N y titulo de cada
capitulo (y, si sirve, su estado o cliffhanger).

MANEJO DE ERRORES (honestidad ante fallos)
Si una herramienta de archivo falla, decilo tal cual y mostra el texto completo en el
chat. NUNCA afirmes que guardaste algo si la escritura fallo.

ENTREGA
Entrega solo la historia, con titulo y "Capitulo N" cuando corresponda.
"""

# ---------------------------------------------------------------------------
# Orquestador: harness con herramientas acotadas y subagentes como tools.
#
# El "human-in-the-loop" NO es un gate de aprobacion: es el chat por turnos. El agente
# escribe un capitulo, lo guarda y termina su turno (lo pide el prompt). El control
# vuelve a la persona, que lee/edita los archivos y en su proximo mensaje decide
# "revisar" o "continuar". No hace falta `interventions` para esto.
# ---------------------------------------------------------------------------


def build_narrador():
    return create_harness(
        model=f"google/{GEMINI_MODEL_ID}",  # orquestador en Gemini; el harness resuelve el string
        effort=EFFORT,
        instructions=SYSTEM_PROMPT,
        # Solo lectura/escritura/edicion de archivos. Todo lo demas, apagado.
        builtin_tools={
            "*": False,
            "read": True,
            "write": True,
            "edit": True,
            # desactivados explicitamente: shell, web_fetch, web_search,
            # programmatic_tool_caller y el subagente generico integrado.
        },
        # Herramientas de delegacion:
        #  - continuidad / estructura: Agents as Tools (revisores, devuelven reporte).
        #  - bibliotecario: tool @tool con structured output (devuelve biblia validada).
        tools=[continuidad.as_tool(), estructura.as_tool(), bibliotecario],
        # Memoria de largo plazo apagada: el estado vive en archivos y la sesion.
        memory=False,
        # Caching y manejo de contexto en automatico.
        caching="auto",
        context_manager="auto",
        # Sesion persistida con id estable por historia (reanudable).
        session={"id": "mini-sherpa-historia-1"},
        # Silencia el streaming del modelo en consola (thinking crudo, "Tool #N").
        # Solo se ve lo que imprime main(). Se pasa directo al Agent del SDK.
        callback_handler=None,
    )


def main():
    print("=== mini-Sherpa — Narrador de historias ===")
    print("Escribi tu pedido. El narrador escribe UN capitulo por turno y para;")
    print("despues podes editar los archivos y pedirle 'revisar' o 'continuar'.")
    print("Comandos: 'salir' para terminar.\n")

    # Chat por turnos: el agente nunca encadena capitulos solo; siempre espera tu
    # proximo mensaje. Esa es la pausa human-in-the-loop.
    agent = build_narrador()

    while True:
        try:
            user = input("✍ ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nHasta la proxima.")
            break

        if not user:
            continue
        if user.lower() in {"salir", "exit", "quit"}:
            print("Hasta la proxima.")
            break

        result = agent(user)
        print("\n📖 Narrador:", str(result), "\n")


if __name__ == "__main__":
    main()
