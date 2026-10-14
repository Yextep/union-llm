# Resultado de Union LLM

| Configuracion | Prompt, tokens/s | Generacion, tokens/s |
| --- | ---: | ---: |
| Central, 4 hilos | 24.69 ± 0.73 | 12.41 ± 0.44 |
| 3 trabajadores RPC, 4 hilos cada uno | 11.82 ± 0.94 | 1.47 ± 0.01 |

La generacion distribuida alcanzo 0.12 veces la velocidad del central.
Medidas de este modelo de 0,5B en CPU, con Wi-Fi, SSH y PRoot; dos repeticiones.

## Buffers de los trabajadores RPC

Mayor reserva individual observada en cada trabajador (MiB). Puede contener pesos y otras estructuras; no equivale al RSS total.

| Trabajador | Mayor reserva RPC, MiB |
| --- | ---: |
| central | 90.08 |
| movil2 | 78.59 |
| movil3 | 307.55 |

RPC0 corresponde a central, RPC1 corresponde a movil2, RPC2 corresponde a movil3.

## Actividad real de trabajadores

| Trabajador | CPU acumulada durante carga y ejecucion, s | Pico RSS del proceso RPC, MiB |
| --- | ---: | ---: |
| central | 17.35 | 101.48 |
| movil2 | 14.48 | 95.13 |
| movil3 | 21.64 | 284.66 |

El pico se refiere al trabajador RPC, no incluye el coordinador ni el resto de Android.

## Respuesta real

Pregunta: Explica en una frase que es una red LAN.

```text
Una red LAN (Local Area Network) es un conjunto de dispositivos conectados a través de una red, generalmente en un espacio local. [end of text]
```

Respuesta identica en ambos modos: si.

La comprobacion confirma la ejecucion repartida; este modelo pequeno no demuestra una mejora general de eficiencia.
