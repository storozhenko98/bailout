#!/usr/bin/env python3
"""Render recorded terminal output, accelerating waiting animations only.

Requires agg, ffmpeg, and ffprobe. No model output or commands are rewritten.
"""
import argparse
import json
import hashlib
from pathlib import Path
import re
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--scenario', choices=['setup', 'recovery'], default='setup')
args = parser.parse_args()
recovery = args.scenario == 'recovery'
OUT = ROOT / ('artifacts/recovery-demo' if recovery else 'artifacts/demo')
MEDIA = ROOT / 'site/demo'
stem = 'bailout-demo-v2' if recovery else 'bailout-demo-v1'
MEDIA.mkdir(parents=True, exist_ok=True)
verification = json.loads((OUT/'verified.json').read_text())
assert verification.get('scenario', 'setup') == args.scenario, 'Recording scenario mismatch'
assert verification['cast_sha256'] == hashlib.sha256((OUT/'session.cast').read_bytes()).hexdigest(), 'Recording did not pass independent verification'
rows = [json.loads(line) for line in (OUT/'session.cast').read_text().splitlines()]
header, events = rows[0], rows[1:]
ansi = re.compile(r'\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07]*(?:\x07|\x1b\\)')
spinner = re.compile(r'\s*[⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏]\s+(Thinking|Running Bash|Recovering response)\s+\d+s · ctrl-c to stop\s*')
adjusted, markers = [], []
now, previous, was_wait = 0., 0., False
for event in events:
    timestamp, kind, text = event
    visible = ansi.sub('', text)
    waiting = kind == 'o' and bool(spinner.fullmatch(visible))
    delta = max(0, timestamp-previous)
    # Continuous spinner updates defeat ordinary idle-time limiting. Preserve
    # every output event but play these waiting spans at 16x; other idle gaps
    # are capped at 1.5 seconds. Leave actual command and response text intact.
    now += min(delta, 1.5) / (16 if waiting and was_wait else 1)
    adjusted.append([round(now,6),kind,text])
    if kind == 'm':
        markers.append({'time':round(now,3),'title':text})
    previous, was_wait = timestamp, waiting
with (OUT/'edited.cast').open('w') as f:
    f.write(json.dumps(header)+'\n')
    for event in adjusted:
        f.write(json.dumps(event,ensure_ascii=False)+'\n')
subprocess.run(['agg','--font-family','Menlo','--font-size','20','--line-height','1.3',
    '--theme','github-dark','--fps-cap','24','--idle-time-limit','1000','--last-frame-duration','4',
    str(OUT/'edited.cast'),str(OUT/'terminal.gif')],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
probe = json.loads(subprocess.check_output(['ffprobe','-v','error','-show_streams','-show_format','-of','json',str(OUT/'terminal.gif')]))
w,h = probe['streams'][0]['width'],probe['streams'][0]['height']
width, height = ((w+128+1)//2)*2, ((h+180+1)//2)*2
font = ROOT/'site/fonts/space-grotesk.ttf'
mono = ROOT/'site/fonts/ibm-plex-mono.ttf'
filters = [f'fps=24,pad={width}:{height}:64:120:color=0xf3f5ef',
    'drawbox=x=40:y=30:w=24:h=24:color=0xb7f567:t=fill',
    f'drawtext=fontfile={font}:text=bailout:fontsize=34:fontcolor=0x17201a:x=78:y=22',
    f'drawtext=fontfile={mono}:text=THE HARNESS MEANT TO BE DELETED.:fontsize=15:fontcolor=0x465148:x=w-tw-40:y=35',
    f'drawtext=fontfile={mono}:text={"PREPARED CONFIG FAILURE / REAL REPAIR / WAITS SHORTENED" if recovery else "REAL UBUNTU SESSION / WAITS SHORTENED / NO SIGN-IN RECORDED"}:fontsize=13:fontcolor=0x536055:x=40:y=h-36']
for i,mark in enumerate(markers):
    text_file=OUT/f'chapter-{i}.txt';text_file.write_text(mark['title'])
    until = markers[i+1]['time'] if i+1<len(markers) else float(probe['format']['duration'])+1
    filters.append(f"drawtext=fontfile={font}:textfile={text_file}:fontsize=25:fontcolor=0x17201a:x=40:y=77:enable='between(t,{mark['time']},{until})'")
(OUT/'filter.txt').write_text(','.join(filters))
subprocess.run(['ffmpeg','-y','-v','warning','-i',str(OUT/'terminal.gif'),'-filter_script:v',str(OUT/'filter.txt'),
    '-c:v','libx264','-preset','slow','-crf','19','-pix_fmt','yuv420p','-movflags','+faststart',
    str(MEDIA/f'{stem}.mp4')],check=True)
subprocess.run(['ffmpeg','-y','-v','warning','-i',str(MEDIA/f'{stem}.mp4'),
    '-vf','fps=8,scale=960:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors=128:stats_mode=diff[p];[b][p]paletteuse=dither=bayer:bayer_scale=3',
    '-loop','0',str(MEDIA/f'{stem}.gif')],check=True)
# A real recorded frame showing the initial ask, before the screen scrolls.
poster_time = min(m['time'] for m in markers if m['title']==('Repair your usual agent' if recovery else 'Set up your tools'))+.2
subprocess.run(['ffmpeg','-y','-v','error','-ss',str(poster_time),'-i',str(MEDIA/f'{stem}.mp4'),
    '-frames:v','1',str(MEDIA/f'{stem}.png')],check=True)
metadata = {'original_seconds':events[-1][0], 'edited_seconds':float(probe['format']['duration']),
            'width':width,'height':height,'markers':markers,'editing':'Spinner waits at 16x; other idle gaps capped at 1.5s. Actual terminal output unchanged.'}
(OUT/'edit.json').write_text(json.dumps(metadata,indent=2)+'\n')
shutil.copyfile(OUT/'session.cast', MEDIA/f'{stem}.cast')
print(json.dumps(metadata,indent=2))
for path in sorted(MEDIA.glob(f'{stem}.*')):
    print(path.name,path.stat().st_size,'bytes')
