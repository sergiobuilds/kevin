"""KEVIN as an open-ended agent: it decides what the LED sign shows and what the radio plays.

One OpenAI call per turn, with the last few turns as memory, returns what KEVIN says
and what each device should do. That covers scenes, free text and emoji on the
sign, songs KEVIN composes, and games played over several turns (quiz, rock-paper-
scissors, number guessing, word chain) with the sign as the game board.
"""
from __future__ import annotations

import json
import copy
import math
import re

ANIMS = ['static', 'scroll_left', 'blink', 'snowflake', 'fade']
SCHEMA = {
    'type': 'object', 'additionalProperties': False,
    'required': ['reply', 'led', 'music', 'rps'],
    'properties': {
        'reply': {'type': 'string'},
        'led': {
            'type': 'object', 'additionalProperties': False,
            'required': ['mode', 'text', 'color', 'bg', 'animation', 'preset', 'snow', 'sky', 'score_left', 'score_right'],
            'properties': {
                'mode': {'type': 'string', 'enum': ['keep', 'text', 'scene', 'score', 'clear']},
                'text': {'type': 'string'},
                'color': {'type': 'string'},
                'bg': {'type': 'string'},
                'animation': {'type': 'string', 'enum': ANIMS},
                'preset': {'type': 'string', 'enum': ['christmas_train', 'santa', 'keep']},
                'snow': {'type': 'string', 'enum': ['keep', 'none', 'normal', 'heavy']},
                'sky': {'type': 'string'},
                'score_left': {'type': 'integer'},
                'score_right': {'type': 'integer'},
            },
        },
        'rps': {
            'type': 'object', 'additionalProperties': False, 'required': ['human', 'kevin'],
            'properties': {'human': {'type': 'string', 'enum': ['', 'rock', 'paper', 'scissors']},
                           'kevin': {'type': 'string', 'enum': ['', 'rock', 'paper', 'scissors']}},
        },
        'music': {
            'type': 'object', 'additionalProperties': False,
            'required': ['mode', 'title', 'tempo', 'notes', 'volume'],
            'properties': {
                'mode': {'type': 'string', 'enum': ['keep', 'carol', 'song', 'stop']},
                'title': {'type': 'string'},
                'tempo': {'type': 'integer'},
                'notes': {'type': 'array', 'items': {
                    'type': 'object', 'additionalProperties': False, 'required': ['n', 'b'],
                    'properties': {'n': {'type': 'string'}, 'b': {'type': 'number'}}}},
                'volume': {'type': ['integer', 'null']},
            },
        },
    },
}

SYSTEM = """너는 KEVIN이야. 「나 홀로 집에」의 케빈처럼 영리하고 장난기 많은 열 살 남자아이 AI이고,
집에 있는 옛날 기기를 직접 부려. 반말로, 1~2문장으로 짧고 생생하게 대답해.

네가 움직일 수 있는 기기:
1) 133cm LED 전광판 (가로 144 x 세로 16 픽셀, 한 줄)
   - mode=text: 짧은 글자/이모지 한 줄. 길면 animation=scroll_left. color/bg는 #RRGGBB.
     예) 생일이면 "🎂 생일 축하해 지민아 🎉", 퀴즈면 문제를 짧게, 가위바위보면 "✊ vs ✌️ 내가 이겼다!"
   - mode=scene: 크리스마스 애니메이션 (preset christmas_train 또는 santa, snow, sky)
   - mode=score: 게임 점수판 (score_left=사람, score_right=케빈, 0~99)
   - mode=clear: 끄기 / mode=keep: 그대로
2) 오래된 블루투스 라디오
   - music.mode=carol: 징글벨 캐럴
   - music.mode=song: 네가 직접 작곡하거나 유명한 동요/노래(생일축하, 학교종 등)의 멜로디를
     notes로 써서 틀어. n은 음이름(C4~C6, 샵 #, 플랫 b, 쉼표 R), b는 박자(0.25~4). 8~40음. tempo 80~160.
   - stop: 끄기 / keep: 그대로. volume은 0~100 또는 null.

게임도 할 수 있어: 퀴즈, 가위바위보, 숫자 맞추기, 끝말잇기, 스무고개 등.
- 가위바위보: rps.human에 사람 손, rps.kevin에 네 손을 넣어(판정은 시스템이 해). 네 손을 무작위로 정하고 규칙(가위>보, 보>바위, 바위>가위, 같으면 비김)대로 정확히 판정해.
  전광판에는 "사람 손 vs 케빈 손 결과"를 이모지로(✌️ 가위, ✊ 바위, ✋ 보). 점수를 기억해.
- 퀴즈: 정답이 하나로 분명한 쉬운 문제(수도, 동물, 산수 등). 사람이 답하면 맞았는지 정확히 채점하고 점수판(score)을 갱신해.
- 생일·축하에는 캐럴이 아니라 생일축하 노래 멜로디(song)를 틀어. 캐럴은 크리스마스 요청에만.
- 노래 요청은 그 노래의 실제 멜로디를 정확한 음으로 써.
이전 대화를 기억하고 이어서 진행해. 게임 상태(문제, 정답, 점수)를 전광판에 보여 줘.
할 수 없는 일(배달, 결제, 문 열기 등)은 솔직하게 못 한다고 하고 기기는 keep.
쓰지 않는 필드는 기본값(text "", color "#FFFFFF", bg "#000000", animation "static", preset "keep",
snow "keep", sky "keep", score 0, title "", tempo 120, notes [], volume null, rps는 human ""/kevin "")으로 채워.
"""


def decide(text: str, history: list, state: dict, openai_json, *, judge=True) -> dict:
    convo = '\n'.join(f"{h['who']}: {h['text']}" for h in history[-10:])
    user = json.dumps({'지금까지 대화': convo, '현재 기기 상태': state, '사람이 방금 한 말': text}, ensure_ascii=False)
    import os
    out = openai_json(SYSTEM, user, SCHEMA, 'kevin_turn', timeout=40, model=os.getenv('KEVIN_AGENT_MODEL', 'gpt-5.4'))
    out = validate_turn(out)
    return judge_rps(out) if judge else out


def validate_turn(out):
    """Validate model output before any device or game-state effect."""
    def validate(value, schema):
        kind = schema['type']
        kinds = kind if isinstance(kind, list) else [kind]
        matches = {'object': isinstance(value, dict), 'array': isinstance(value, list),
                   'string': isinstance(value, str), 'integer': type(value) is int,
                   'number': type(value) in (int, float) and math.isfinite(value), 'null': value is None}
        if not any(matches.get(k, False) for k in kinds):
            raise ValueError('invalid agent field type')
        if 'enum' in schema and value not in schema['enum']:
            raise ValueError('unsupported agent field')
        if kind == 'object':
            if set(value) != set(schema['required']):
                raise ValueError('invalid agent fields')
            for key, item in value.items(): validate(item, schema['properties'][key])
        elif kind == 'array':
            if len(value) > 60: raise ValueError('too many notes')
            for item in value: validate(item, schema['items'])
    validate(out, SCHEMA)
    out = copy.deepcopy(out)
    led, music = out['led'], out['music']
    if len(out['reply']) > 1000 or len(led['text']) > 80 or (led['mode'] == 'text' and not led['text'].strip()):
        raise ValueError('invalid text length')
    for key in ('color', 'bg'):
        if not re.fullmatch(r'#[0-9A-Fa-f]{6}', led[key]): raise ValueError('invalid color')
    if led['sky'] != 'keep' and not re.fullmatch(r'#[0-9A-Fa-f]{6}', led['sky']):
        raise ValueError('invalid sky color')
    if any(not 0 <= led[key] <= 99 for key in ('score_left', 'score_right')):
        raise ValueError('invalid score')
    if music['volume'] is not None and not 0 <= music['volume'] <= 100:
        raise ValueError('invalid volume')
    if not 60 <= music['tempo'] <= 200 or len(music['title']) > 200:
        raise ValueError('invalid song')
    if music['mode'] == 'song' and not music['notes']:
        raise ValueError('song requires notes')
    for note in music['notes']:
        if not re.fullmatch(r'(?:[A-G][#b]?[3-6]|R)', note['n']) or not .125 <= note['b'] <= 4:
            raise ValueError('invalid note')
    return out


HAND = {'rock': ('바위', '✊'), 'paper': ('보', '✋'), 'scissors': ('가위', '✌️')}
BEATS = {'rock': 'scissors', 'paper': 'rock', 'scissors': 'paper'}
SCORE = {'human': 0, 'kevin': 0}


def judge_rps(out, score=None):
    """Rock-paper-scissors is judged by rule, not by the model."""
    import random
    score = SCORE if score is None else score
    h = out['rps']['human']
    if h not in HAND:
        return out
    k = random.choice(list(HAND))  # KEVIN never sees your hand first
    out['rps']['kevin'] = k
    if h == k:
        result, line = 'draw', '비겼다! 한 판 더?'
    elif BEATS[k] == h:
        result, line = 'kevin', '내가 이겼다~ ㅋㅋ'
        score['kevin'] += 1
    else:
        result, line = 'human', '으악, 네가 이겼어! 한 판 더!'
        score['human'] += 1
    out['reply'] = f"난 {HAND[k][0]}! {line} ({score['human']}:{score['kevin']})"
    tag = {'draw': '비김', 'kevin': '케빈 승', 'human': '너 승'}[result]
    out['led'].update(mode='text', text=f"{HAND[h][1]} vs {HAND[k][1]} {tag} {score['human']}:{score['kevin']}", animation='static',
                      color='#FFD75C', bg='#000000')
    out['rps_result'] = result
    return out
