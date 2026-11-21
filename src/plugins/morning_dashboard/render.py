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


def hourly_outlook(info, now):
    # The probability at 09:00 belongs to the interval ending at 09:00.
    first = int(now.timestamp()) // 3600 * 3600
    ends = dict((info or {}).get("rain", ()))
    starts = [datetime.fromtimestamp(first + i * 3600, now.tzinfo) for i in range(6)]
    hours = [stamp.strftime("%H:%M") for stamp in starts]
    return [(stamp.strftime("%H %Z") if hours.count(label) > 1 else label, ends.get(first + (i + 1) * 3600))
            for i, (stamp, label) in enumerate(zip(starts, hours))]


def draw_dashboard(size, location, now, weather, agenda):
    width, height = size
    portrait = width < height
    scale = min(width / 480, height / 680) if portrait else min(width / 760, height / 455)

    def px(value):
        return max(1, round(value * scale))

    image = Image.new("RGB", size, "white")
    pen = ImageDraw.Draw(image)
    margin = px(20)
    right = width - margin
    usable = right - margin
    blue = "#153f78"
    bold = {17, 22, 29, 46}
    fonts = {n: get_font("Jost", px(n), "bold" if n in bold else "normal")
             for n in (13, 15, 16, 17, 19, 22, 29, 46)}

    def line(x, y, text, n, space, color="black"):
        font = fonts[n]
        pen.text((x, y), fitted(text, font, space), font=font, fill=color, anchor="lt")

    location = " ".join(location.split())[:100]
    location = fitted(location, fonts[19], usable * 0.30)
    location_width = fonts[19].getlength(location)
    line(margin, margin, f"{DAYS[now.weekday()]} {now.day} {MONTHS[now.month - 1]}", 29,
         usable - location_width - px(12))
    pen.text((right, margin + px(6)), location, font=fonts[19], fill=blue, anchor="rt")
    y = margin + px(43)
    pen.line((margin, y, right, y), fill="black", width=px(2))
    y += px(14)
    info, stamp, stale = weather
    rail_x = margin if portrait else margin + round(usable * 0.45)
    left_space = usable if portrait else rail_x - margin - px(14)
    if info:
        temp = f"{info['temperature']}°"
        line(margin, y, temp, 46, left_space)
        after_temp = margin + round(fonts[46].getlength(temp)) + px(10)
        line(after_temp, y + px(17), CONDITIONS.get(info["code"], "Meteo variabile"), 19,
             margin + left_space - after_temp)
        day = date.fromisoformat(info["day"]).strftime("%d/%m")
        line(margin, y + px(55), f"{day}   Min {info['low']}°   Max {info['high']}°   ·   Percepita {info['feels']}°",
             16, left_space)
        details = f"{info['pressure']} hPa"
        visibility = info.get("visibility_m")
        if visibility is not None:
            distance = f"{visibility} m" if visibility < 1000 else f"{visibility / 1000:.0f} km"
            details += f"   ·   Visibilità {distance}"
        line(margin, y + px(80), details, 16, left_space)
    else:
        line(margin, y + px(15), "Meteo non disponibile", 22, left_space)

    rail_y = y + px(110) if portrait else y + px(3)
    rail_width = right - rail_x
    line(rail_x, rail_y, "Probabilità di precipitazioni", 17, rail_width, blue)
    periods = hourly_outlook(info, now)
    if any(value is not None for _, value in periods):
        for i, (label, value) in enumerate(periods):
            x = rail_x + rail_width * i // 6
            bar_w = rail_width // 6 - px(7)
            line(x, rail_y + px(29), label, 13, bar_w)
            bar_y = rail_y + px(50)
            if value is not None:
                pen.rectangle((x, bar_y, x + bar_w, bar_y + px(7)), outline="black")
                fill_w = round(bar_w * value / 100)
                if fill_w:
                    pen.rectangle((x, bar_y, x + fill_w, bar_y + px(7)), fill=blue)
            line(x, rail_y + px(66), "—" if value is None else f"{value}%", 17, bar_w)
    else:
        line(rail_x, rail_y + px(39), "Previsione oraria non disponibile", 16, rail_width)

    y = rail_y + px(105)
    source = "Open-Meteo"
    if info:
        source += datetime.fromisoformat(info["at"]).strftime(" %d/%m %H:%M")
    line(margin, y, freshness(stamp, stale) + "   ·   " + source, 15, usable)
    y += px(30)
    pen.line((margin, y, right, y), fill="black", width=px(2))
    y += px(14)
    line(margin, y, "Agenda di oggi", 22, usable)
    y += px(32)
    if not agenda.total:
        line(margin, y, "Nessun calendario configurato", 19, usable)
        return image
    if not agenda.covered:
        line(margin, y, "Agenda di oggi non disponibile", 19, usable)
        return image
    status = agenda.status()
    if fonts[15].getlength(status) > usable and " · " in status:
        counts, status = status.rsplit(" · ", 1)
        line(margin, y, counts, 15, usable, blue)
        y += px(22)
    line(margin, y, status, 15, usable, blue)
    y += px(30)
    if not agenda.items:
        message = ("Nessun impegno in calendario" if agenda.covered == agenda.total
                   else "Nessun impegno nei calendari disponibili")
        line(margin, y, message, 19, usable)
    else:
        row_h = px(31)
        capacity = max(1, (height - margin - y) // row_h)
        show = capacity if len(agenda.items) <= capacity else capacity - 1
        upcoming = next((item.start for item in agenda.items if item.state == "upcoming"), None)
        for item in agenda.items[:show]:
            tag = {"ongoing": "In corso", "past": "Concluso"}.get(item.state, "")
            if item.state == "upcoming" and item.start == upcoming:
                tag = "Prossimo"
            time_width = px(109)
            line(margin, y, item.label, 16, time_width - px(5))
            tag_width = round(fonts[15].getlength(tag)) + px(12) if tag else 0
            line(margin + time_width, y, item.title, 17, usable - time_width - tag_width)
            if tag:
                pen.text((right, y + px(1)), tag, font=fonts[15], fill=blue, anchor="rt")
            y += row_h
        if len(agenda.items) > show:
            line(margin, y, f"Altri impegni: {len(agenda.items) - show}", 16, usable)
    return image
