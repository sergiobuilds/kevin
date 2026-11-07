"""144x16 frame model and deterministic offline scene interpreter."""
import base64
import copy
from collections import Counter
import io
import json
import re
import urllib.error
import urllib.request
from PIL import Image, ImageSequence

WIDTH, HEIGHT = 144, 16
COLORS = {'black':'#000000','검정':'#000000','purple':'#8000c0','보라':'#8000c0','blue':'#0033aa','파랑':'#0033aa','파란':'#0033aa','red':'#ff2020','빨강':'#ff2020','빨간':'#ff2020','green':'#00aa44','초록':'#00aa44','white':'#ffffff','하양':'#ffffff','하얀':'#ffffff','yellow':'#ffcc00','노랑':'#ffcc00','노란':'#ffcc00'}
class PixelError(ValueError): pass

def validate(scene):
    if not isinstance(scene, dict) or not isinstance(scene.get('frames'), list) or not 1 <= len(scene['frames']) <= 32: raise PixelError('1–32 frames required')
    duration = scene.get('duration', 200)
    if type(duration) is not int or not 40 <= duration <= 5000: raise PixelError('duration must be 40–5000 ms')
    for frame in scene['frames']:
        if not isinstance(frame,list) or len(frame)!=HEIGHT: raise PixelError('frame height must be 16')
        for row in frame:
            if not isinstance(row,list) or len(row)!=WIDTH or any(not isinstance(c,str) or not re.fullmatch(r'#[0-9a-fA-F]{6}',c) for c in row): raise PixelError('each row must contain 144 #RRGGBB pixels')
    return {'frames':scene['frames'], 'duration':duration}

def blank(color='#000000'):
    return [[color for x in range(WIDTH)] for y in range(HEIGHT)]

def sky_color(frame):
    # A painted corner must not be mistaken for the background. This heuristic
    # remains approximate for arbitrary imported images.
    return Counter(color for row in frame[:4] for color in row).most_common(1)[0][0]

def generate(prompt, current=None, trees=None):
    if not isinstance(prompt, str) or not prompt.strip():
        raise PixelError('prompt required')
    text = prompt.lower()
    color = next((v for k, v in COLORS.items() if k in text), None)
    edit_words = ('바꿔', '바꾸', '변경', 'change', '수정', '색을', '더 많이',
                  '더 내려', '많이 내려', '추가', '지워', '없애', '줄여', '제거',
                  '더 적', 'more ', 'less ', 'add ', 'remove ')
    new_words = ('만들', '그려', 'create', 'draw', 'new scene', '새 장면')
    mentions_sky = any(word in text for word in ('하늘', 'sky', '배경', 'background'))
    editing = current is not None and (any(word in text for word in edit_words) or
        (color is not None and mentions_sky and not any(word in text for word in new_words)))
    if editing:
        # Work on a copy: a rejected edit must not damage the previous scene.
        scene = copy.deepcopy(validate(current))
        sky_requested = mentions_sky
        more_snow = any(s in text for s in ('눈', 'snow')) and any(
            s in text for s in ('더 많이', '더 내려', '많이 내려', 'more', 'increase'))
        if any(word in text for word in ('나무를', '집을', '마을을', 'tree', 'house', 'village')) and not sky_requested:
            raise PixelError('기존 장면의 요소 추가·제거·색 변경은 아직 지원하지 않습니다')
        if color is None and not more_snow:
            raise PixelError('기존 장면은 하늘 색 변경과 눈을 더 내리는 수정만 지원합니다')
        if color:
            for frame in scene['frames']:
                old = sky_color(frame)
                for y in range(HEIGHT):
                    for x in range(WIDTH):
                        # Preserve houses, trees, snow and manually painted pixels.
                        if frame[y][x] == old and (not sky_requested or y < 13):
                            frame[y][x] = color
        if more_snow:
            original = scene['frames']
            # Preserve imported animation length; animate a still image over a full snow cycle.
            count = 13 if len(original) == 1 else len(original)
            scene['frames'] = []
            changed = 0
            for t in range(count):
                frame = copy.deepcopy(original[t % len(original)])
                background = sky_color(frame)
                added = 0
                for i in range(WIDTH):
                    x, y = (i * 53 + 13) % WIDTH, (i * 5 + (t * 13 // count)) % 13
                    if frame[y][x] == background and background.lower() != '#ffffff':
                        frame[y][x] = '#ffffff'
                        added += 1
                        changed += 1
                        if added == 48:
                            break
                scene['frames'].append(frame)
            if not changed:
                raise PixelError('눈을 추가할 수 있는 배경이 없습니다. 배경색을 바꿔주세요')
        return validate(scene)
    sky = color or '#091537'
    snow = any(s in text for s in ('눈', 'snow'))
    village = any(s in text for s in ('마을', '집', 'village', 'house', '크리스마스', 'christmas'))
    trees = (village or any(s in text for s in ('나무', 'tree'))) if trees is None else trees
    if not (color or snow or village or trees or any(s in text for s in ('하늘', '배경', 'sky', 'background'))):
        raise PixelError('오프라인에서는 마을, 집, 나무, 눈, 하늘과 색상만 지원합니다')
    frames = []
    for t in range(13 if snow else 1):
        f = blank(sky)
        if village:
            for x in range(WIDTH):
                for y in range(13, 16): f[y][x] = '#deedff'
            for start in range(5, WIDTH-9, 18):
                for y in range(8, 13):
                    for x in range(start, start+10): f[y][x] = '#ad493d'
                for y in range(4, 8):
                    for x in range(start+7-y, start+3+y): f[y][x] = '#e0664f'
                for y in (10, 11):
                    for x in (start+2, start+3, start+6, start+7): f[y][x] = '#ffdf70'
        if trees:
            for center in range(20, WIDTH, 18):
                for y in range(6, 13):
                    half = (y - 6) // 2
                    for x in range(max(0, center-half), min(WIDTH, center+half+1)):
                        f[y][x] = '#16764a'
                f[13][center] = '#754529'
        if snow:
            for i in range(32):
                x, y = (i*37+7) % WIDTH, (i*7+t) % 13
                if f[y][x] == sky: f[y][x] = '#ffffff'
        frames.append(f)
    return {'frames': frames, 'duration': 200}

def import_image(data):
    try:
        raw=base64.b64decode(data,validate=True)
        if len(raw)>4_000_000: raise PixelError('image too large')
        im=Image.open(io.BytesIO(raw))
        first_duration = im.info.get('duration', 200) or 200
        if im.width*im.height>4_000_000: raise PixelError('image dimensions too large')
        frames=[]
        for frame in ImageSequence.Iterator(im):
            if len(frames)>=32: raise PixelError('maximum 32 frames')
            rgb=frame.convert('RGBA'); background=Image.new('RGBA',rgb.size,(0,0,0,255)); background.alpha_composite(rgb)
            rgb=background.convert('RGB').resize((WIDTH,HEIGHT),Image.Resampling.NEAREST)
            frames.append([['#%02x%02x%02x'%rgb.getpixel((x,y)) for x in range(WIDTH)] for y in range(HEIGHT)])
        return validate({'frames':frames,'duration':max(40,min(5000,int(first_duration)))})
    except PixelError: raise
    except Exception as e: raise PixelError('invalid image') from e

def export_image(scene, kind):
    scene=validate(scene); images=[]
    for frame in scene['frames']:
        im=Image.new('RGB',(WIDTH,HEIGHT)); im.putdata([tuple(bytes.fromhex(c[1:])) for row in frame for c in row]); images.append(im)
    out=io.BytesIO()
    if kind=='gif': images[0].save(out,format='GIF',save_all=True,append_images=images[1:],duration=scene['duration'],loop=0,disposal=2)
    elif kind=='png': images[0].save(out,format='PNG')
    else: raise PixelError('unsupported format')
    return base64.b64encode(out.getvalue()).decode()

class _NoProviderRedirect(urllib.request.HTTPRedirectHandler):
    """Never forward a provider's bearer token to a redirected endpoint."""

    def redirect_request(self, request, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(request.full_url, code, 'provider redirects are not allowed', headers, fp)


def provider_scene(url, token, prompt, current):
    """Optional provider contract: POST prompt/current, return validated scene JSON."""
    if not url.startswith('https://'): raise PixelError('provider must use HTTPS')
    req=urllib.request.Request(url,json.dumps({'prompt':prompt,'current':current,'width':WIDTH,'height':HEIGHT}).encode(),{'Content-Type':'application/json','Authorization':'Bearer '+token})
    try:
        with urllib.request.build_opener(_NoProviderRedirect()).open(req, timeout=20) as response:
            return validate(json.loads(response.read(2_000_001)))
    except Exception as e: raise PixelError('provider failed: '+str(e)) from e
