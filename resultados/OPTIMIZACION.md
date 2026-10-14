# Optimizacion de Union LLM

Datos generados por `tools/informe-optimizacion.py` desde `resultados/optimizacion/`.
Mediciones con `llama-bench` (prompt de 64 tokens, 32 generados) salvo que se indique otra cosa.

## a) CPU del central, Qwen2.5-Coder-3B Q4_K_M

| Compilacion e hilos | Prompt, tok/s | Generacion, tok/s |
| --- | ---: | ---: |
| ARMv8-A generica, 4 hilos (anterior) | 2.62 ± 0.51 | 3.81 ± 0.06 |
| dotprod, 4 hilos | 6.04 ± 4.01 | 4.80 ± 0.12 |
| dotprod, 2 hilos en los nucleos rapidos | 11.55 ± 0.08 | 6.37 ± 0.01 |
| dotprod, 6 hilos | 7.60 ± 7.43 | 4.99 ± 0.01 |
| dotprod, 8 hilos | 14.51 ± 0.07 | 5.43 ± 0.07 |

## a) Qwen2.5-Coder-3B repartido entre central, movil3 y movil4 (Wi-Fi anterior)

| Variante | Reparto (capas) | Prompt, tok/s | Generacion, tok/s |
| --- | --- | ---: | ---: |
| Compilacion anterior, 4 hilos, central por RPC | central 14, movil4 7, movil3 15 | 3.75 ± 0.01 | 0.82 ± 0.13 |
| dotprod, nucleos rapidos, central por RPC | central 13, movil4 8, movil3 15 | 4.14 ± 0.07 | 0.99 ± 0.06 |
| dotprod, nucleos rapidos, central en su CPU | central 13, movil4 9, movil3 14 | 5.34 ± 0.29 | 0.93 ± 0.01 |
| Diagnostico: todo en movil3 | central 0, movil3 36 | 3.18 ± 0.18 | 1.25 ± 0.02 |
| Diagnostico: todo en el central, por RPC local | central 36 | 8.65 ± 0.01 | 5.67 ± 0.13 |

## b) Qwen2.5-Coder-7B Q4_K_M: no cabe en ningun dispositivo solo

| Variante | Reparto (capas) | Prompt, tok/s | Generacion, tok/s |
| --- | --- | ---: | ---: |
| Minimo de dispositivos, Wi-Fi anterior | central 10, movil2 18 | 4.08 ± 0.00 | 1.22 ± 0.00 |
| Todos los dispositivos, Wi-Fi anterior | central 6, movil4 4, movil2 10, movil3 8 | 2.93 ± 0.00 | 0.49 ± 0.00 |
| Minimo de dispositivos, Wi-Fi 5 GHz | central 8, movil3 20 | 1.83 ± 0.00 | 1.13 ± 0.00 |

## d) Modelos grandes en el chat real: denso frente a MoE

Wi-Fi 5 GHz. Minimo de dispositivos, temperatura 0, sin razonamiento previo, 64 tokens por respuesta.
La carga es la del ultimo intento: parte de las capas ya estaba en la cache de los moviles.

| Modelo | Reparto (capas) | Carga, min | Pregunta | Prompt, tok/s | Generacion, tok/s |
| --- | --- | ---: | --- | ---: | ---: |
| Qwen3.8-27B denso, UD-IQ2_XXS (7,3 GB) | central 12, movil2 31, movil3 21 | 5 | 1 | 0.33 | 0.27 |
| Qwen3.8-27B denso, UD-IQ2_XXS (7,3 GB) | central 12, movil2 31, movil3 21 | 5 | 2 | 0.24 | 0.27 |
| ERNIE-4.5-21B-A3B MoE, UD-Q2_K_XL (8,2 GB) | central 6, movil3 11, movil2 11 | 7 | 1 | 3.63 | 2.18 |
| ERNIE-4.5-21B-A3B MoE, UD-Q2_K_XL (8,2 GB) | central 6, movil3 11, movil2 11 | 7 | 2 | 3.39 | 2.23 |

**Qwen3.8-27B denso, UD-IQ2_XXS (7,3 GB).** Escribe una funcion en Python que devuelva los numeros primos menores que n usando la criba de Eratostenes. Incluye un comentario breve.

~~~text
```python
def primos_menores_que_n(n: int) -> list[int]:
    """Devuelve los números primos menores que n usando la criba de Eratóstenes."""
    if n <= 2:
        return []

    # Inicializar lista de booleanos:
~~~

**Qwen3.8-27B denso, UD-IQ2_XXS (7,3 GB).** Explica en tres frases que es una red LAN y para que sirve.

~~~text
Una red LAN (Local Area Network) es una infraestructura de comunicación que conecta dispositivos dentro de un área geográfica limitada, como una oficina o un edificio. Su función principal es permitir la comunicación eficiente y de bajo costo entre los equipos, compartiendo recursos como impresoras, servidores y acceso a internet. Finalmente,
~~~

**ERNIE-4.5-21B-A3B MoE, UD-Q2_K_XL (8,2 GB).** Escribe una funcion en Python que devuelva los numeros primos menores que n usando la criba de Eratostenes. Incluye un comentario breve.

~~~text
```python
def sieve_of_eratosthenes(n):
    """
    Devuelve una lista de números primos menores que n usando el algoritmo de la criba de Eratostenes.
    """
    if n <= 2:
~~~

**ERNIE-4.5-21B-A3B MoE, UD-Q2_K_XL (8,2 GB).** Explica en tres frases que es una red LAN y para que sirve.

~~~text
Una **Red Local Area Network (LAN)** es una red de computadoras o dispositivos conectados físicamente en un área limitada, como una oficina, escuela o hogar.  
Sirve para permitir la comunicación y compartición de recursos, como archivos, imp
~~~

## Red: Wi-Fi anterior frente a 5 GHz

Viaje de ida y vuelta de un mensaje pequeno por el tunel SSH (mediana / percentil 90, ms).

| Movil | Wi-Fi anterior | Wi-Fi 5 GHz |
| --- | ---: | ---: |
| movil2 | 13.69 / 44.03 | 6.6 / 11.18 |
| movil3 | 36.65 / 70.69 | 7.16 / 9.47 |
| movil4 | 32.77 / 58.92 | 10.21 / 14.25 |

Ancho de banda medido del central a un movil: 1,5-1,8 MB/s antes y 15-17 MB/s en 5 GHz,
con y sin SSH. La carga inicial de un modelo de 7 GB paso de mas de una hora a 5-7 minutos.

## Decodificacion especulativa, 3B repartido (chat real, temperatura 0, Wi-Fi anterior)

| Variante | Pregunta | Generacion, tok/s | Borrador aceptado |
| --- | --- | ---: | ---: |
| Sin borrador | 1 | 1.01 | — |
| Sin borrador | 2 | 0.98 | — |
| Borrador Qwen2.5-0.5B, hasta 8 tokens | 1 | 0.69 | 99/215 |
| Borrador Qwen2.5-0.5B, hasta 8 tokens | 2 | 0.46 | 41/168 |
