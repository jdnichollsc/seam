# character_swap.api.json

SHA-256 `4db7670a58574e0c2def6f48dad3e00f7a5dda8ba37501bb92d5361ab47552b0`.

Copied unmodified from `workflows/swap.api.json` of
[Comfy-Org/comfy-openjutsu](https://github.com/Comfy-Org/comfy-openjutsu) (MIT License, Copyright (c) 2026
Comfy Org), which builds on Akatz Labs' H3 Character Swap workflow (MIT License, Copyright (c) 2026 Akatz Labs).
The same MIT text below applies under both copyright lines. Seam fills the graph in at run time
(`src/seam/swap.py`); the file itself is not edited, and a test pins its hash.

The graph uses node classes of the `h3_character_swap` pack from
[akatz-labs/h3-character-swap-tool](https://github.com/akatz-labs/h3-character-swap-tool) (GPL-3.0). That pack is
NOT included in Seam and no code from it is copied here. The user installs it in their own ComfyUI.

```
MIT License

Copyright (c) 2026 Comfy Org
Copyright (c) 2026 Akatz Labs

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```
