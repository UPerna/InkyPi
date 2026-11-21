from datetime import date, datetime
from PIL import Image, ImageDraw
from utils.app_utils import get_font

DAYS = ("Lunedì", "Martedì", "Mercoledì", "Giovedì", "Venerdì", "Sabato", "Domenica")
MONTHS = ("gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno", "luglio",
          "agosto", "settembre", "ottobre", "novembre", "dicembre")
CONDITIONS = {0: "Sereno", 1: "Prevalentemente sereno", 2: "Poco nuvoloso", 3: "Coperto",
              45: "Nebbia", 48: "Nebbia con brina", 51: "Pioviggine", 53: "Pioviggine",
              55: "Pioviggine intensa", 56: "Pioviggine gelata", 57: "Pioviggine gelata",
              61: "Pioggia debole", 63: "Pioggia", 65: "Pioggia intensa", 66: "Pioggia gelata",
              67: "Pioggia gelata", 71: "Neve debole", 73: "Neve", 75: "Neve intensa",
              77: "Granelli di neve", 80: "Rovesci", 81: "Rovesci", 82: "Rovesci intensi",
              85: "Rovesci di neve", 86: "Rovesci di neve", 95: "Temporale",
              96: "Temporale con grandine", 99: "Temporale con grandine"}


def agenda_rows(data, now):
    day = now.date()
    if not data or not data["first"] <= day.isoformat() < data["until"]:
        return None
    rows = []
    for event in data["events"]:
        if event["allDay"]:
            start = date.fromisoformat(event["start"])
            end = date.fromisoformat(event["end"]) if event.get("end") else None
            if not (start == day or (end and start < day < end)):
                continue
            order, label = (0, 0), "Tutto il giorno"
        else:
            start = datetime.fromisoformat(event["start"]).astimezone(now.tzinfo)
            end = datetime.fromisoformat(event.get("end", event["start"])).astimezone(now.tzinfo)
            if start.date() != day and not (start.date() < day and end.timestamp() >
                                            now.replace(hour=0, minute=0, second=0, microsecond=0, fold=0).timestamp()):
                continue
            label = start.strftime("%H:%M") if start.date() == day else "In corso"
            order = (1, start.timestamp())
        title = " ".join(event["title"].split())[:512] or "Senza titolo"
        rows.append((order, label, title))
    return [(label, title) for _, label, title in sorted(rows)]


def fitted(text, font, width):
    if font.getlength(text) <= width:
        return text
    left, right = 0, len(text)
    while left < right:
        middle = (left + right + 1) // 2
        if font.getlength(text[:middle] + "…") <= width:
            left = middle
        else:
            right = middle - 1
    return text[:left] + "…"


def freshness(stamp, stale):
    if stamp is None:
        return "In attesa dei dati"
    prefix = "Dati salvati" if stale else "Aggiornato"
    return prefix + stamp.strftime(" %d/%m/%Y %H:%M")


def draw_dashboard(size, location, now, weather, agenda):
    width, height = size
    scale = min(width / 720, height / 445)
    def px(n):
        return max(1, round(n * scale))

    image = Image.new("RGB", size, "white")
    pen = ImageDraw.Draw(image)
    margin = px(18)
    usable = width - 2 * margin
    fonts = {n: get_font("Jost", px(n), "bold" if n in (24, 30) else "normal")
             for n in (15, 19, 21, 24, 30)}

    def line(y, value, n=21, x=margin, space=usable):
        pen.text((x, y), fitted(value, fonts[n], space), font=fonts[n], fill="black", anchor="lt")

    line(margin, f"{DAYS[now.weekday()]} {now.day} {MONTHS[now.month - 1]}", 30)
    y = margin + px(49)
    info, stamp, stale = weather
    location = " ".join(location.split())[:100]
    line(y, f"Meteo · {location}", 24)
    source = "Open-Meteo"
    if info:
        source += datetime.fromisoformat(info["at"]).strftime(" %d/%m %H:%M")
    line(y + px(30), freshness(stamp, stale) + " · " + source, 15)
    if info:
        line(y + px(55), f"{info['temperature']} °C   {CONDITIONS.get(info['code'], 'Meteo variabile')}", 30)
        forecast_date = date.fromisoformat(info["day"]).strftime("%d/%m")
        details = (f"{forecast_date}   Min {info['low']}°   Max {info['high']}°"
                   f"    ·    Percepita {info['feels']}°    ·    {info['pressure']} hPa")
        visibility = info.get("visibility_m")
        if visibility is not None:
            distance = f"{visibility} m" if visibility < 1000 else f"{visibility / 1000:.0f} km"
            details += f"    ·    Visibilità {distance}"
        line(y + px(95), details, 19)
    else:
        line(y + px(62), "Meteo non disponibile")

    y += px(135)
    pen.line((margin, y, width - margin, y), fill="black", width=px(1))
    y += px(13)
    line(y, "Agenda di oggi", 24)
    if agenda is None:
        line(y + px(40), "Nessun calendario configurato")
        return image
    data, stamp, stale = agenda
    line(y + px(30), freshness(stamp, stale), 15)
    y += px(58)
    rows = agenda_rows(data, now)
    if rows is None:
        line(y, "Agenda di oggi non disponibile")
    elif not rows:
        line(y, "Nessun impegno in calendario")
    else:
        step = px(32)
        capacity = max(1, (height - margin - y) // step)
        show = capacity if len(rows) <= capacity else capacity - 1
        column = px(133)
        for label, title in rows[:show]:
            line(y, label, 19, margin, column - px(5))
            line(y, title, 21, margin + column, usable - column)
            y += step
        if len(rows) > show:
            line(y, f"Altri impegni: {len(rows) - show}", 19)
    return image
