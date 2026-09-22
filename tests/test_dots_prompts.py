from core.dots_prompts import local_reference


def test_priority_mapping_and_explicit_selection(tmp_path):
    (tmp_path / 'a.wav').write_bytes(b'placeholder')
    (tmp_path / 'b.wav').write_bytes(b'placeholder')
    (tmp_path / 'prompt_text').write_text('a|A的参考文本\nb|B的参考文本\n', encoding='utf-8-sig')
    assert local_reference('', tmp_path) == (str(tmp_path / 'a.wav'), 'A的参考文本')
    assert local_reference('b.wav', tmp_path) == (str(tmp_path / 'b.wav'), 'B的参考文本')
    (tmp_path / 'b.txt').write_text('同名文本优先', encoding='utf-8')
    assert local_reference('b.wav', tmp_path)[1] == '同名文本优先'
    assert local_reference('unknown.wav', tmp_path) is None
    assert local_reference('../elsewhere.wav', tmp_path) is None


def test_empty_directory_falls_back(tmp_path):
    assert local_reference('', tmp_path) is None
