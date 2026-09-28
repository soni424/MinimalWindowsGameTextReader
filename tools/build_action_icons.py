"""Build small, theme-aware line icons for Tk buttons at common DPI scales."""
from pathlib import Path
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1] / 'assets' / 'action_icons'
SIZES = (16, 24, 32)
COLORS = {'dark': '#edf3fb', 'light': '#172033', 'primary': '#ffffff'}
NAMES = ('play', 'pause', 'previous', 'next', 'capture', 'snippet', 'stop',
         'repeat', 'copy', 'delete', 'add', 'edit', 'settings', 'help',
         'check', 'cancel', 'record', 'folder', 'voice', 'save', 'image')


def render(name: str, size: int, color: str) -> Image.Image:
    scale = size / 24
    canvas = Image.new('RGBA', (size, size))
    draw = ImageDraw.Draw(canvas)
    def point(x, y): return (round(x * scale), round(y * scale))
    def line(*coords):
        draw.line([point(*p) for p in coords], fill=color, width=max(1, round(2 * scale)), joint='curve')
    def rect(a, b, radius=0):
        draw.rounded_rectangle((*point(*a), *point(*b)), radius=round(radius * scale),
                               outline=color, width=max(1, round(2 * scale)))
    def circle(a, b):
        draw.ellipse((*point(*a), *point(*b)), outline=color, width=max(1, round(2 * scale)))
    if name == 'play': line((8, 5), (18, 12), (8, 19), (8, 5))
    elif name == 'pause':
        line((9, 5), (9, 19)); line((15, 5), (15, 19))
    elif name in ('previous', 'next'):
        if name == 'previous': line((6, 5), (6, 19)); line((18, 5), (8, 12), (18, 19))
        else: line((18, 5), (18, 19)); line((6, 5), (16, 12), (6, 19))
    elif name == 'capture':
        for x,y,dx,dy in ((4,4,5,0),(20,4,-5,0),(4,20,5,0),(20,20,-5,0)):
            line((x,y),(x+dx,y)); line((x,y),(x,y+(5 if y == 4 else -5)))
    elif name == 'snippet': circle((4,4),(20,20)); line((9,12),(15,12))
    elif name == 'stop': rect((6,6),(18,18),2)
    elif name == 'repeat':
        line((18,8),(18,5),(14,5)); line((18,5),(20,7));
        draw.arc((*point(4,5),*point(20,19)), 220, 510, fill=color, width=max(1,round(2*scale)))
    elif name == 'copy': rect((7,7),(18,20),1); line((5,16),(5,4),(16,4))
    elif name == 'delete': rect((7,7),(17,20),1); line((5,5),(19,5)); line((9,3),(15,3))
    elif name == 'add': circle((4,4),(20,20)); line((12,7),(12,17)); line((7,12),(17,12))
    elif name == 'edit': line((5,18),(8,19),(19,8),(16,5),(5,16),(5,18))
    elif name == 'settings': circle((8,8),(16,16)); circle((3,3),(21,21))
    elif name == 'help': circle((3,3),(21,21)); line((9,9),(12,7),(15,9),(12,12),(12,14)); circle((11,17),(13,19))
    elif name == 'check': line((4,12),(10,18),(20,6))
    elif name == 'cancel': line((5,5),(19,19)); line((19,5),(5,19))
    elif name == 'record': circle((6,6),(18,18))
    elif name == 'folder': line((3,19),(3,7),(9,7),(11,10),(21,10),(21,19),(3,19))
    elif name == 'voice':
        rect((9,3),(15,15),3); line((5,12),(5,15),(9,19),(15,19),(19,15),(19,12)); line((12,19),(12,22))
    elif name == 'save': rect((5,4),(19,20),2); rect((8,4),(16,10)); rect((8,14),(16,20))
    elif name == 'image': rect((3,5),(21,19),2); circle((15,8),(18,11)); line((4,17),(10,11),(17,18))
    return canvas


def build() -> None:
    for theme, color in COLORS.items():
        for size in SIZES:
            target = ROOT / theme / str(size)
            target.mkdir(parents=True, exist_ok=True)
            for name in NAMES:
                render(name, size, color).save(target / f'{name}.png', optimize=True)


if __name__ == '__main__':
    build()
