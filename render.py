import sys
import matplotlib
matplotlib.use('Agg')
matplotlib.rcParams['mathtext.fontset'] = 'stix'
matplotlib.rcParams['font.family'] = 'STIXGeneral'
import matplotlib.pyplot as plt
import numpy as np

try:
    out, expr, result, tag = sys.argv[1:5]
    DPI = 200

    def mt(s):
        return '$' + s + '$'

    def measure(s, size):
        fig = plt.figure(dpi=DPI)
        t = fig.text(0, 0, mt(s), fontsize=size)
        fig.canvas.draw()
        bb = t.get_window_extent(fig.canvas.get_renderer())
        plt.close(fig)
        return bb.width, bb.height

    S1, S2 = 30.0, 48.0
    res = result[1:] if result.startswith('@') else '= ' + result
    w1, h1 = measure(expr, S1)
    w2, h2 = measure(res, S2)
    maxw = max(w1, w2)
    if maxw > 2200:
        k = 2200.0 / maxw
        S1, S2 = S1 * k, S2 * k
        w1, h1 = measure(expr, S1)
        w2, h2 = measure(res, S2)
        maxw = max(w1, w2)

    pad, gap = 90, 40
    W = max(maxw + 2 * pad, 700)
    H = h1 + h2 + gap + 2 * pad

    fig = plt.figure(figsize=(W / DPI, H / DPI), dpi=DPI)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.axis('off')
    grad = np.linspace(0.5, 0.0, 512)[None, :]
    ax.imshow(grad, aspect='auto', cmap='gray', vmin=0, vmax=1, extent=[0, 1, 0, 1])

    y1 = H - pad - h1 / 2
    y2 = pad + h2 / 2
    fig.text(0.5, y1 / H, mt(expr), ha='center', va='center', fontsize=S1, color=(1, 1, 1, 0.85))
    fig.text(0.5, y2 / H, mt(res), ha='center', va='center', fontsize=S2, color='white')
    if tag:
        fig.text(24.0 / W, 1 - 24.0 / H, tag, ha='left', va='top', fontsize=10, color=(1, 1, 1, 0.5))
    fig.savefig(out, dpi=DPI, facecolor='black')
except Exception as e:
    sys.stderr.write('render.py error: %r\n' % (e,))
    sys.exit(1)
