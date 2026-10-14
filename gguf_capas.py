"""Tamano en bytes de cada capa de un GGUF, leyendo solo su cabecera."""
import mmap
from pathlib import Path
import struct
import sys

# Tipos escalares de metadatos GGUF y su formato para struct.
SCALARS = {0: 'B', 1: 'b', 2: 'H', 3: 'h', 4: 'I', 5: 'i', 6: 'f', 7: '?', 10: 'Q', 11: 'q', 12: 'd'}
STRING, ARRAY = 8, 9


def layout(path):
    """Devuelve la arquitectura, los bytes por capa y los tensores de entrada y salida."""
    with Path(path).open('rb') as stream, mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ) as data:
        position = 0

        def read(fmt):
            nonlocal position
            values = struct.unpack_from('<' + fmt, data, position)
            position += struct.calcsize('<' + fmt)
            return values[0] if len(values) == 1 else values

        def text():
            nonlocal position
            size = read('Q')
            position += size
            return data[position - size:position].decode('utf-8', errors='replace')

        def skip(kind):
            nonlocal position
            if kind == STRING:
                text()
            elif kind == ARRAY:
                inner, count = read('IQ')
                if inner in SCALARS:
                    position += struct.calcsize('<' + SCALARS[inner]) * count
                elif inner == STRING:
                    # Los vocabularios tienen cientos de miles de textos: se saltan sin decodificar.
                    for _ in range(count):
                        position += 8 + struct.unpack_from('<Q', data, position)[0]
                else:
                    for _ in range(count):
                        skip(inner)
            else:
                position += struct.calcsize('<' + SCALARS[kind])

        if data[:4] != b'GGUF':
            raise ValueError(f'{path} no es un archivo GGUF.')
        position = 4
        version, tensors, entries = read('IQQ')
        if version < 2:
            raise ValueError('Version GGUF demasiado antigua.')
        metadata = {}
        for _ in range(entries):
            key = text()
            kind = read('I')
            if kind in SCALARS:
                metadata[key] = read(SCALARS[kind])
            elif kind == STRING:
                metadata[key] = text()
            else:
                skip(kind)
        infos = []
        for _ in range(tensors):
            name = text()
            dimensions = read('I')
            position += 8 * dimensions + 4
            infos.append((read('Q'), name))
        alignment = metadata.get('general.alignment', 32)
        data_size = len(data) - (position + alignment - 1) // alignment * alignment
    architecture = metadata['general.architecture']
    layers = [0] * metadata[architecture + '.block_count']
    sizes = {}
    infos.sort()
    # Los datos estan contiguos: el tamano de cada tensor llega hasta el siguiente.
    for (offset, name), (end, _) in zip(infos, infos[1:] + [(data_size, '')]):
        sizes[name] = end - offset
    entry = output = 0
    for name, size in sizes.items():
        if name.startswith('blk.'):
            index = int(name.split('.')[1])
            if index < len(layers):
                layers[index] += size
            else:
                output += size
        elif name.startswith('output'):
            output += size
        else:
            entry += size
    if 'output.weight' not in sizes:
        # Con embeddings compartidos, llama.cpp copia token_embd al dispositivo de salida.
        output += sizes.get('token_embd.weight', 0)
    return {'arquitectura': architecture, 'capas': layers, 'salida': output, 'entrada': entry,
            'expertos': metadata.get(architecture + '.expert_count', 0)}


if __name__ == '__main__':
    for argument in sys.argv[1:]:
        info = layout(argument)
        mib = 1024 * 1024
        print(f"{Path(argument).name}: {info['arquitectura']}, {len(info['capas'])} capas, "
              f"{sum(info['capas']) / mib:.0f} MiB en capas, salida {info['salida'] / mib:.0f} MiB, "
              f"entrada {info['entrada'] / mib:.0f} MiB, expertos {info['expertos']}")
