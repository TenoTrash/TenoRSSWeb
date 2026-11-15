# TenoRSSWeb

Lector de RSS/Atom que genera una web para leer las noticias desde el navegador.

Versión web de [TenoRSS](https://github.com/). Solo Python 3, sin dependencias
externas (todo con la biblioteca estándar).

## Uso

```bash
python3 TenoRSSWeb.py
```

Descarga las fuentes de `rss.txt` y sirve la web en <http://localhost:8080>
(Ctrl+C para salir).

Opciones:

| Opción | Qué hace |
|---|---|
| `--port 9000` | Usar otro puerto (por defecto 8080). |
| `--update` | Solo descargar y actualizar `cache.json`, sin servir. |
| `--build index.html` | Generar un HTML estático y salir. |
| `--no-update` | Arrancar el servidor sin descargar al inicio. |
| `--interval 15` | Minutos entre actualizaciones automáticas (por defecto 30; `0` las desactiva). |

Mientras el servidor está levantado refresca las fuentes solo cada 30 minutos
(configurable con `--interval`). Igual el botón **Actualizar** las vuelve a
bajar en el momento. Se puede marcar un ítem (o una fuente entera) como leído,
y el estado queda guardado en `cache.json`. Arranca en **vista de mosaico**
(grilla de tarjetas con la imagen del ítem como miniatura; los que no traen
imagen muestran solo título y fecha) y un botón del encabezado la alterna con
la **vista de lista**; al abrir una tarjeta se expande a todo el ancho con el
contenido.
La vista elegida y el tema claro/oscuro se recuerdan en el navegador (el tema
sigue al del sistema por defecto).

Cada ítem tiene también un botón **Guardar**: archiva esa noticia en
`saved.json` y aparece en la vista **★ Guardados** de la barra lateral. Ver
["Guardados"](#guardados-bookmarks-savedjson) más abajo.

## Fuentes: `rss.txt`

Una sección `[Nombre]` por feed, cada una con su línea `URL="..."`.

```ini
[Xataka]
URL="https://www.xataka.com/index.xml"

[Hacker News]
URL="https://hnrss.org/frontpage"
```

Las líneas que empiezan con `#` son comentarios.

No hay nada de zona horaria para configurar: las fechas de cada ítem se
guardan en UTC y el navegador las muestra en tu hora local.

## Cache: `cache.json`

Guarda hasta 50 ítems por fuente, con el estado leído/no leído. Se crea y se
reescribe solo, así que no hace falta tocarlo (ni versionarlo). Si se borra, se
regenera en la próxima actualización.

Formato (JSON válido, un ítem por línea):

```json
{"app":"TenoRSSWeb","version":1,"saved":<epoch>,"feeds":[
{"name":"...","url":"...","updated":<epoch>,"status":"ok","items":[
{"id":"a1b2c3d4","read":0,"date":<epoch>,"title":"...","link":"...","text":"..."}
]}
]}
```

## Guardados (bookmarks): `saved.json`

El botón **Guardar** de cada ítem archiva esa noticia en `saved.json` y la
muestra en la vista **★ Guardados** de la barra lateral. A diferencia de
`cache.json`, este archivo **no se poda ni se regenera**: guarda una copia
(*snapshot*) del ítem al momento de guardarlo, así la noticia no se pierde
aunque envejezca y salga del caché, o aunque borres la fuente.

- La copia conserva título, enlace, contenido, fecha, la **fuente de origen** y
  cuándo se guardó.
- Las imágenes de lo guardado también quedan en `media/` (la limpieza las
  respeta), así se ven offline.
- Es **data personal**: está en `.gitignore` (no va al repo ni al zip) y, como
  no se regenera sola, **conviene respaldarla**. Para quitar un guardado, usá
  el mismo botón (**Guardada ★**) en la noticia.

Formato (igual que `cache.json`, un ítem por línea):

```json
{"app":"TenoRSSWeb","version":1,"items":[
{"id":"a1b2c3d4","date":<epoch>,"saved_ts":<epoch>,"feed":"...","title":"...","link":"...","text":"..."}
]}
```

## Qué entiende

- RSS 2.0, RSS 1.0 (RDF) y Atom, con o sin namespaces.
- Prefiere `content:encoded` / `content` sobre `description` (el texto más completo).
- Guarda el contenido como **HTML saneado** (lista blanca de etiquetas: párrafos,
  negritas, listas, enlaces, imágenes…; se descartan `script`, `style`, `iframe`,
  atributos de eventos, `javascript:`, etc.). Los enlaces abren en pestaña nueva.
- Recorta el contenido a `TEXT_LIMIT` caracteres visibles (por defecto 3000),
  cerrando bien las etiquetas abiertas.
- Ordena los ítems del más nuevo al más viejo cuando todos traen fecha.
- Conserva el estado de leído al actualizar.
- Si una fuente falla, deja sus ítems anteriores y muestra el error sin afectar
  al resto.

En la web cada ítem muestra solo el título; al hacer clic se despliega el
contenido y un enlace al artículo original.

## Lectura offline

Al actualizar, las imágenes de cada noticia (las del cuerpo y la destacada de
Media RSS: `media:content` / `media:thumbnail` / `enclosure` de tipo imagen) se
descargan a la carpeta `media/` y se reescriben a rutas locales (`/media/…`).
Así, una vez actualizado, el lector funciona sin conexión: el HTML, el CSS, el
JavaScript y las tipografías ya son locales, y ahora también las imágenes.

- Cada imagen se guarda una sola vez (nombre = hash de la URL).
- No se cachean imágenes de más de 5 MB (`MAX_IMG_BYTES`); quedan como enlace
  remoto (se ven solo con conexión).
- Las imágenes que dejan de usarse (ítems que salen del caché o fuentes
  borradas) se eliminan solas en la siguiente actualización.

Lo único que sigue necesitando conexión es "Abrir enlace original" (es el
artículo completo en su sitio).

## Estructura

- `TenoRSSWeb.py`: todo el programa (config, descarga, parser, cache, web y servidor).
- `rss.txt`: las fuentes a leer.
- `cache.json`: ítems de cada fuente (se regenera solo).
- `saved.json`: noticias guardadas (bookmarks); data personal, no se versiona
  ni se regenera (respaldala).
- `media/`: imágenes cacheadas para lectura offline (se regenera; no se versiona).
- `backups/`: copias de `rss.txt` previas a cada edición desde `/fuentes`.

## Licencia

MIT. Ver [LICENSE](LICENSE).
