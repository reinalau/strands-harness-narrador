# Requerimientos — Narrador de historias (mini-Sherpa)

Se quiere implementar, con **Strands Agents Harness**, un narrador de historias
pequeño inspirado en https://pocketfm.com/sherpa, pero que **solo produce texto**
(sin audio ni imágenes). La salida es siempre en **español**.

> **Restricción transversal:** todo se implementa **dentro del SDK de Strands harness**,
> sin mecanismos por fuera. Las herramientas se usan a través del harness
> (`builtin_tools`, subagentes como tool, etc.), se **desactiva** lo que no se use, y
> se **setean explícitamente** todos los parámetros relevantes de `create_harness(...)`.

## Arquitectura: conjunto de agentes

Un **agente orquestador** (narrador) coordina el flujo y **delega subtareas a tres
subagentes expuestos como herramientas** (patrón "Agents as Tools" del harness:
cada subagente es un `Agent` con nombre/descripción/system prompt, pasado como
`tools=[... .as_tool()]`).

- **Orquestador / Narrador (principal):** conduce la entrevista inicial, decide modo
  (cuento/novela), **escribe la prosa**, pide/aplica dirección del usuario, entrega el
  texto, y **es el ÚNICO que escribe en disco** (`write`/`edit`). Es el único que
  "habla" con el usuario.
- **Subagente Continuidad (solo análisis):** lee la biblia + el capítulo nuevo y
  **reporta** contradicciones, conocimiento imposible e hilos perdidos. No escribe.
- **Subagente Estructura/Ritmo (solo análisis):** audita que el texto respete las
  etapas narrativas (ver "Definiciones de estructura narrativa"). **Reporta**. No escribe.
- **Subagente Bibliotecario (solo análisis, structured output):** **genera** la biblia
  actualizada como un objeto **validado por Pydantic** (structured output) y la
  **devuelve como JSON**; el **orquestador** lo escribe en disco. No escribe por sí mismo.

### Modelos y rol de escritura (dos modelos)

- **Orquestador:** Google **Gemini** (`google/gemini-2.5-flash`), `effort="off"` (único
  nivel que ese modelo acepta en el harness). Escribe la prosa y es el **único que
  escribe en disco** (`write`/`edit`).
- **Subagentes:** **Nova Lite** en Bedrock (`us.amazon.nova-lite-v1:0`), más barato.
  Son de **solo análisis**: no tocan disco, así que el bug de rutas de Windows de Nova
  no los afecta. Devuelven texto (reportes) o JSON validado (biblia).
- **Gemini escribe todo:** una sola fuente de escritura en disco; los subagentes nunca
  persisten.

### Cómo se construyen los agentes (patrones documentados del SDK)

- **Orquestador:** `create_harness(...)` — hereda caching, gestión de contexto, prompt
  afinado y demás optimizaciones del harness.
- **continuidad / estructura (revisores):** patrón **"Agents as Tools"** (doc: Delegate
  to subagents): `strands.Agent(name, description, system_prompt, model=BedrockModel(...))`
  + `.as_tool()`. Son `Agent` "clásicos" (sin las optimizaciones del harness), lo cual
  alcanza porque devuelven un reporte de una sola pasada.
- **bibliotecario:** usa **structured output** (doc: Return structured output): un
  `Agent` Nova invocado con `structured_output_model=Biblia` (modelo Pydantic), expuesto
  al orquestador como una tool `@tool`. Devuelve un JSON **validado** contra el esquema,
  en vez de texto libre frágil. El orquestador escribe ese JSON en `bible.json`.
- **Agent-loop:** cada agente (orquestador y subagentes) corre su propio bucle del SDK
  (modelo → herramienta → resultado → modelo). Los revisores, al no tener herramientas,
  resuelven en una sola iteración; el orquestador hace el bucle largo (escribe, edita,
  delega).

> Nota: a un `Agent` crudo hay que pasarle una **instancia de modelo** (`GeminiModel` /
> `BedrockModel`), no el string `"google/..."` / `"bedrock/..."` — ese string solo lo
> resuelve `create_harness()`.

## Human-in-the-loop (chat por turnos, no un gate de aprobación)

El human-in-the-loop **no** es un gate de aprobación (`interventions`); es el **chat por
turnos**. El orquestador escribe **un** capítulo (o el cuento), lo guarda y **termina su
turno** — nunca encadena el siguiente por su cuenta. El control vuelve a la persona, que:

- **Edita a mano** los archivos generados: el capítulo/cuento (`.md`) y/o la biblia
  (`bible.json`), directamente en el editor.
- En su próximo mensaje decide: pedir **revisar** o **continuar**.
  - **Revisar:** el orquestador **relee los archivos desde disco** (nunca asume que su
    contexto es lo último, la persona pudo editarlos), delega en **Continuidad** y
    **Estructura/Ritmo**, reporta/corrige, y marca el capítulo como `revisado`.
  - **Continuar:** el orquestador relee `bible.json` y el último capítulo antes de
    escribir el siguiente.

La fuente de verdad son **los archivos en disco**. La "pausa" la da el loop de chat (el
agente siempre espera el próximo mensaje) + la regla de "un capítulo por turno" en el
prompt. No requiere `interventions`.

## Estado por generación (`state.json`)

Además de la sesión, los capítulos y la biblia, hay un archivo de estado por historia
`state.json` que marca el avance y permite el informe de capítulos:

```json
{
  "modo": "novela",
  "ultimo_capitulo": 1,
  "capitulos": [
    {
      "n": 1,
      "titulo": "La carta que no llegó",
      "archivo": "story/capitulo-01.md",
      "estado": "revisado",
      "cliffhanger": ""
    }
  ]
}
```

Estados posibles de un capítulo:

- `generado` — lo escribió el agente, sin tocar.
- `editado_por_usuario` — la persona lo modificó a mano.
- `revisado` — pasó por los subagentes de revisión **después** de la última edición.

Lo lee/escribe el orquestador con sus herramientas de archivo (nativo del SDK).

## Requerimientos funcionales

1. **Entrevista inicial:** el orquestador pregunta formato (cuento o novela), género,
   tono, personajes (nombre, rol, objetivo, relaciones), premisa, punto de vista y
   extensión, **en un solo mensaje breve y numerado**. Si el usuario no decide algo,
   propone un valor y avanza.
2. **Dos modos:**
   - **Cuento:** un solo pase, 1200–2500 palabras. Sin archivos de estado por defecto;
     **pero** si la persona quiere editar y pedir revisión, el cuento se guarda en
     `story/cuento.md` para poder editarlo (ver human-in-the-loop).
   - **Novela:** por capítulos, 800–1500 palabras cada uno.
3. **Chat reanudable:** cerrar y volver a la misma historia con el historial intacto,
   reabriendo con el **mismo `session` id**.
4. **Biblia de la historia (solo novela) — en JSON validado:** `bible.json` con
   personajes, relaciones ("desde el capítulo N"), qué sabe cada uno, hilos abiertos,
   objetos y línea de tiempo. La **genera** el subagente Bibliotecario vía **structured
   output** (modelo Pydantic `Biblia`), garantizando un JSON válido; la **escribe** el
   orquestador al cerrar cada capítulo. Esquema (equivalente a los modelos Pydantic):
   ```json
   {
     "meta": {"titulo": "", "genero": "", "tono": "", "punto_de_vista": "", "ultimo_capitulo": 0},
     "personajes": [{"nombre": "", "rol": "", "objetivo": "", "desde_capitulo": 1}],
     "relaciones": [{"a": "", "b": "", "tipo": "", "desde_capitulo": 1}],
     "quien_sabe_que": [{"personaje": "", "sabe": "", "desde_capitulo": 1}],
     "hilos_abiertos": [{"descripcion": "", "abierto_en_capitulo": 1, "cerrado_en_capitulo": null}],
     "objetos": [{"nombre": "", "descripcion": "", "desde_capitulo": 1}],
     "linea_de_tiempo": [{"capitulo": 1, "eventos": [""]}]
   }
   ```
   Relaciones como **lista de aristas** (grafo "aplanado"); convertible a grafo real en v2.
5. **Capítulos como archivos:** uno por capítulo en `story/capitulo-NN.md`.
6. **Dirección del usuario:** puede orientar el próximo capítulo o pedir correcciones;
   se aplican al texto y a la biblia.
7. **Revisión antes de entregar / a pedido:** vía subagentes Continuidad y
   Estructura/Ritmo. Los subagentes **solo reportan**; el orquestador decide qué
   corregir (no "arregla" por su cuenta lo que la persona puso a propósito).
8. **Informe de capítulos:** si la persona lo pide, el orquestador entrega una
   **enumeración con títulos** (N + título + opcional estado/cliffhanger), leída de
   `state.json` (rápido, sin abrir cada archivo).
9. **Salida solo en texto**, en español, con título y "Capítulo N".
10. **Honestidad ante fallos:** si guardar un archivo falla, lo dice y entrega el texto
    completo en el chat; nunca afirma que guardó algo si falló.

## Definiciones de estructura narrativa (reglas de escritura + base del subagente)

Estas definiciones van **en el prompt** del orquestador (para escribir bien de entrada)
y son el **criterio del subagente Estructura/Ritmo** (para auditar después).

### Cuento (estructura lineal y concentrada)

Formato breve: pocos personajes, un solo conflicto principal, todo pasa rápido.

- **Introducción / Inicio:** presenta protagonista, escenario y equilibrio inicial.
  Puede ser cortísima o arrancar *in media res*.
- **Incidente desencadenante:** un hecho rompe la normalidad y obliga a actuar.
- **Nudo / Desarrollo:** el personaje intenta resolver; la tensión sube rápido.
- **Clímax:** máxima tensión; el conflicto se decide.
- **Desenlace:** resolución, a menudo abrupta/impactante o abierta. El equilibrio
  inicial cambia por completo.

### Novela por capítulos (estructura compleja y fractal)

**Macro-estructura (toda la novela):**

- **Acto 1 — Planteamiento (~20–25%):** mundo, personajes, vidas. Termina con un punto
  de giro que cambia el rumbo.
- **Acto 2 — Confrontación (~50% central):** obstáculos crecientes. A mitad, un "punto
  de no retorno" (punto medio).
- **Acto 3 — Resolución (~20–25%):** clímax general y cierre de subtramas.

**Micro-estructura (cada capítulo, mini-cuento con trampa final):**

- **Inicio:** objetivo inmediato del personaje para la escena.
- **Nudo:** intenta lograrlo y se topa con un problema o revelación.
- **Desenlace / Cliffhanger:** en vez de cerrar, el final queda abierto o con una nueva
  complicación para obligar a pasar de página.

### Reglas de ritmo adicionales

- **Mostrar en vez de contar.**
- **Densidad escena vs resumen:** dramatizar (diálogo/acción momento a momento) los
  momentos clave; resumir el paso del tiempo entre ellos.
- **Entrar tarde / salir temprano:** empezar la escena lo más cerca posible del
  conflicto y cortar apenas se resuelve.

### Cuadro comparativo

| Característica | Cuento | Capítulo de novela |
|---|---|---|
| Conflicto | Único y central | Secundario; avanza la trama principal |
| Ritmo | Rápido, directo | Más pausado, explora detalles |
| Final | Resuelve la duda (cierre) | Genera nueva pregunta/peligro (gancho) |

## Requerimientos no funcionales (configuración del harness)

Todo se setea explícitamente en `create_harness(...)`:

- **Herramientas (`builtin_tools`):** partir de `"*": False` y habilitar **solo**
  `read`, `write`, `edit` en el orquestador. **Desactivados:** shell, web
  (`web_fetch`/`web_search`), `programmatic_tool_caller` y el subagente genérico
  integrado (`subagent`), ya que usamos subagentes propios vía `as_tool()`.
- **Subagentes:** tres `Agent` propios (Continuidad, Estructura/Ritmo, Bibliotecario)
  pasados en `tools=[...]`, **solo lectura/razonamiento** (sin `write`/`edit`/shell/web).
- **Modelos (dos):** orquestador en `google/gemini-2.5-flash`; subagentes en
  `us.amazon.nova-lite-v1:0` (Bedrock), más barato (solo análisis, no tocan disco).
- **Razonamiento (`effort`):** `off`. Verificado empíricamente: `gemini-2.5-flash`
  **solo** acepta `off` en el harness (rechaza `low` y `medium` con
  `400 Thinking level is not supported`).
- **Costo:** `caching` automático y `context_manager="auto"`.
- **Memoria de largo plazo:** **apagada** (`memory=False`).
- **Sesiones:** activas, con `id` estable por historia (`session={"id": ...}`).
- **Interventions:** sin puertas de aprobación para pedidos de escritura. Se evalúa un
  **límite de turnos** por solicitud para evitar bucles.
- **Windows / UTF-8:** forzar UTF-8 en la salida del proceso para no crashear al
  imprimir caracteres no-ascii.

## Tres (cuatro) capas de estado

1. **Sesión:** historial de la conversación (reanudable por `id`). La da el harness.
2. **Capítulos:** archivos `story/capitulo-NN.md` (y `story/cuento.md` si aplica).
3. **Biblia:** `bible.json` (solo novela).
4. **Estado:** `state.json` (avance, títulos, estado por capítulo).

La **memoria de largo plazo** del harness queda **apagada** en v1.

## Decisiones de diseño

- **v1:** una sola historia en `story/` (más `bible.json` y `state.json`). El esquema
  `stories/<id>/` por historia queda para v2.
- **Biblia en JSON** (no grafo); relaciones como lista de aristas. La genera el
  bibliotecario con **structured output** (Pydantic), para que el JSON sea siempre válido.
- **Dos modelos:** orquestador en Gemini (escribe la prosa y en disco); subagentes en
  **Nova Lite** (análisis barato, sin tocar disco).
- **Tres subagentes separados:** Continuidad y Estructura/Ritmo vía **Agents as Tools**
  (`Agent` + `.as_tool()`); Bibliotecario vía **tool `@tool` con structured output**.

## Fuera de alcance para v1

Voz, imágenes, interfaz web, múltiples usuarios, memoria entre historias, simulación de
oyentes, ajuste fino de modelos, y el esquema multi-historia `stories/<id>/`.

## Cómo saber que funciona

1. Un **cuento** corto completo.
2. Una **novela** de tres capítulos, comprobando que el tercero respete `bible.json`.
3. **Editar a mano** un capítulo y la biblia, pedir **revisión**, y verificar que el
   orquestador relee desde disco y reporta sobre la versión editada.
4. Pedir un **informe de capítulos** y recibir la enumeración con títulos.
5. **Cerrar** el proceso, **reanudar** con el mismo `session` id y continuar.
6. **Forzar un fallo de escritura** y verificar que el agente **no** diga que guardó.

## Referencia

Papers de Pocket FM / Sherpa (a incorporar selectivamente):
narrative-world-model.pdf, personas-to-plot.pdf, adaptation-not-translation.pdf,
pause.pdf, listener-agents.pdf, vividworld.pdf.

> Nota: estos PDFs no se leyeron automáticamente (el fetch no acepta `application/pdf`).
> Para incorporar sus ideas, pegar fragmentos clave como texto.
