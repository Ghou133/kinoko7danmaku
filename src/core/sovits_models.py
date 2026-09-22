"""Discover paired weights without loading executable model checkpoints."""

import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ModelPair:
    name: str
    version: str
    gpt: str
    sovits: str

    @property
    def label(self) -> str:
        return f'{self.name} · {self.version}'


def model_name(path: Path) -> str:
    # GPT: name-e15.ckpt; SoVITS: name_e6_s2370_l32.pth.
    return re.sub(r'[-_]e\d+(?:[-_]s\d+)?(?:[-_]l\d+)?$', '', path.stem, flags=re.I)


def scan_models(folder: str) -> tuple[list[ModelPair], list[str]]:
    root = Path(folder).expanduser()
    if not folder or not (root / 'api_v2.py').is_file():
        raise ValueError('请选择包含 api_v2.py 的 GPT-SoVITS 安装文件夹')
    pairs, issues = [], []
    for version in ('v1', 'v2', 'v2Pro', 'v2ProPlus', 'v3', 'v4'):
        suffix = '' if version == 'v1' else '_' + version
        gpts = sorted((root / ('GPT_weights' + suffix)).rglob('*.ckpt'))
        sovits = sorted((root / ('SoVITS_weights' + suffix)).rglob('*.pth'))
        used = set()
        for gpt in gpts:
            exact = [s for s in sovits if s.stem == gpt.stem]
            matches = exact or [s for s in sovits if model_name(s) == model_name(gpt)]
            if len(matches) != 1:
                issues.append(f'{version}/{gpt.name}: ' + ('缺少 SoVITS' if not matches else '多个 SoVITS，无法确定配对'))
                continue
            s = matches[0]
            used.add(s)
            pairs.append(ModelPair(model_name(gpt), version, str(gpt.resolve()), str(s.resolve())))
        issues.extend(f'{version}/{s.name}: 没有配对 GPT' for s in sovits if s not in used)
    return pairs, issues
