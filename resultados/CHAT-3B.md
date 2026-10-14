# Prueba real del chat distribuido

Fecha: 13 de octubre de 2026. Modelo: Qwen2.5-Coder-3B-Instruct Q4_K_M.
Una sola instancia de llama-server, un slot de generacion, contexto de 2048
tokens y tres trabajadores RPC en CPU. El motor permanece cargado entre
preguntas. Esta prueba no compara el modelo de 3B con ejecucion en un solo
telefono y no permite afirmar una mejora de velocidad.

Pregunta inicial:

> Responde solo con un bloque de codigo Python que defina sumar(a, b) y devuelva a + b.

Respuesta recibida progresivamente por la API del chat:

```python
def sumar(a, b):
    return a + b
```

Seguimiento, enviando la conversacion anterior como contexto:

> Que devuelve sumar(2, 3)? Responde solo el numero.

Respuesta: **5**.

| Medicion | Funcion Python | Seguimiento |
| --- | ---: | ---: |
| Primer token desde la solicitud | 30,68 s | 9,82 s |
| Solicitud completa, incluye procesamiento del prompt | 56,57 s | 12,96 s |
| Tokens generados, segun el motor | 18 | 2 |

El motor registro 3,30 tokens/s al procesar el primer prompt y 0,66 tokens/s
en su fase de generacion. En el seguimiento reutilizo 118 tokens de contexto;
solo proceso 26 tokens nuevos. Las mediciones anteriores excluyen la carga
inicial del modelo, que fue mucho mas lenta.

| Trabajador | Aumento de tiempo CPU durante la primera pregunta | RSS al terminar |
| --- | ---: | ---: |
| central | 28,51 s | 630,1 MiB |
| movil2 | 30,09 s | 582,6 MiB |
| movil3 | 40,34 s | 800,0 MiB |

El aumento de CPU verifica calculo durante la respuesta en los tres dispositivos.
Estos tiempos CPU no son tiempos de espera de una persona; suman la actividad
de los hilos de cada proceso. RSS incluye los datos residentes del trabajador,
no el coordinador ni el resto del sistema operativo. Las capas se ejecutan en
orden, por lo que no se ocupa toda la CPU de los tres a la vez para cada token.

Los logs del motor registran los siguientes buffers del modelo y cache KV:

| Destino | Pesos del modelo | KV |
| --- | ---: | ---: |
| CPU del coordinador | 166,92 MiB | — |
| RPC central | 577,63 MiB | 26,00 MiB |
| RPC movil2 | 518,99 MiB | 24,00 MiB |
| RPC movil3 | 738,20 MiB | 22,00 MiB |

Los tamanos de buffers son asignaciones del backend; no equivalen al uso total
de RAM del telefono. El texto "GPU" en algunos mensajes genericos de offload
de llama.cpp corresponde aqui a los backends RPC configurados con `--device CPU`;
esta prueba no usa una GPU.

La API del chat tambien fue accesible desde los dos moviles mediante HTTP.
Las cuatro pruebas HTTP con un motor simulado pasaron, incluida la cancelacion
y el rechazo de una segunda generacion simultanea. La logica del navegador
paso las pruebas DOM. La revision visual automatica quedo pendiente porque
Chromium termino con SIGTRAP en PRoot; eso no impidio servir el chat ni realizar
estas dos solicitudes reales.

Datos originales: [chat-prueba-real.json](chat-prueba-real.json).
Logs: `logs/chat-motor.log`, `logs/chat-rpc-central.log`,
`logs/chat-rpc-movil2.log` y `logs/chat-rpc-movil3.log`.
