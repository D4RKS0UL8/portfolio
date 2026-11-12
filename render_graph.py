import sys, json, warnings
import numpy as np
warnings.filterwarnings('ignore')
np.seterr(all='ignore')
import matplotlib
matplotlib.use('Agg')
matplotlib.rcParams['mathtext.fontset'] = 'stix'
matplotlib.rcParams['font.family'] = 'STIXGeneral'
import matplotlib.pyplot as plt
from matplotlib.path import Path
from matplotlib.patches import PathPatch
from matplotlib.colors import LinearSegmentedColormap

BG = '#0c0c10'
BLUE = '#6fd3ff'
PINK = '#ff7eb6'


def fail(msg):
    sys.stderr.write('GRAPHMSG:' + msg + '\n')
    sys.exit(2)


def arr(v):
    return np.array(v, dtype=float)


def glow(ax, x, y, color, lw=2.6):
    for w, a in ((13, 0.05), (8, 0.09), (4.5, 0.17)):
        ax.plot(x, y, color=color, lw=w, alpha=a, solid_capstyle='round', solid_joinstyle='round', zorder=2)
    ax.plot(x, y, color=color, lw=lw, solid_capstyle='round', solid_joinstyle='round', zorder=3)


def main():
    out, jpath = sys.argv[1], sys.argv[2]
    d = json.load(open(jpath))
    kind = d['kind']
    show_axes = d.get('axes', True)

    fig = plt.figure(figsize=(9, 6.2), dpi=160, facecolor=BG)
    rect = [0.085, 0.08, 0.885, 0.73] if show_axes else [0.02, 0.02, 0.96, 0.80]
    ax = fig.add_axes(rect)
    ax.set_facecolor(BG)
    if show_axes:
        ax.grid(True, color='white', alpha=0.08, lw=0.8)
        for sp in ax.spines.values():
            sp.set_color((1, 1, 1, 0.25))
        ax.tick_params(colors=(1, 1, 1, 0.6), labelsize=10)
        ax.axhline(0, color='white', alpha=0.35, lw=1.1, zorder=1)
        ax.axvline(0, color='white', alpha=0.35, lw=1.1, zorder=1)
    else:
        ax.axis('off')

    if kind == 'explicit':
        x = arr(d['x'])
        y = arr(d['y'])
        fin = y[np.isfinite(y)]
        if fin.size < 2:
            fail('Nothing to plot in this range')
        lo, hi = float(fin.min()), float(fin.max())
        p1, p99 = np.percentile(fin, [1, 99])
        span = float(p99 - p1)
        full = hi - lo
        if full < 1e-9:
            lo, hi = lo - 1, hi + 1
        elif span > 0 and full > 3 * span:
            lo, hi = p1 - 0.25 * span, p99 + 0.25 * span
        else:
            lo, hi = lo - 0.08 * full, hi + 0.08 * full
        y[(y < lo) | (y > hi)] = np.nan
        jump = np.abs(np.diff(y)) > 0.6 * (hi - lo)
        y[1:][jump] = np.nan
        glow(ax, x, y, BLUE)
        ax.set_xlim(d['xlim'][0], d['xlim'][1])
        ax.set_ylim(lo, hi)

    elif kind == 'curve':
        x = arr(d['x'])
        y = arr(d['y'])
        m = np.isfinite(x) & np.isfinite(y)
        if m.sum() < 2:
            fail('Nothing to plot for that range')
        x[~m] = np.nan
        y[~m] = np.nan
        x0, x1, y0, y1 = x[m].min(), x[m].max(), y[m].min(), y[m].max()
        pad = 0.12 * max(x1 - x0, y1 - y0, 1e-9)
        glow(ax, x, y, PINK)
        ax.set_xlim(x0 - pad, x1 + pad)
        ax.set_ylim(y0 - pad, y1 + pad)
        ax.set_aspect('equal', adjustable='datalim')

    elif kind == 'implicit':
        n = int(d['n'])
        x0, x1, y0, y1 = d['win']
        Z = np.ma.masked_invalid(arr(d['z']).reshape(n, n))
        if Z.count() == 0:
            fail('Nothing to plot in this window')
        X = np.linspace(x0, x1, n)
        Y = np.linspace(y0, y1, n)
        cs = ax.contour(X, Y, Z, levels=[0], colors=['k'], alpha=0)
        segs = cs.allsegs[0]
        if not segs:
            fail('No curve found in this window (try: from -10 to 10)')
        heart = bool(d.get('fill'))
        if heart:
            verts, codes = [], []
            for s in segs:
                if len(s) < 3:
                    continue
                verts += s.tolist()
                codes += [Path.MOVETO] + [Path.LINETO] * (len(s) - 1)
                verts.append(s[0].tolist())
                codes.append(Path.CLOSEPOLY)
            if verts:
                patch = PathPatch(Path(verts, codes), facecolor='none', edgecolor='none')
                ax.add_patch(patch)
                cmap = LinearSegmentedColormap.from_list('h', ['#7a0010', '#d90429', '#ff4d6d', '#ffb3c1'])
                grad = np.linspace(0, 1, 256)[:, None]
                im = ax.imshow(grad, extent=[x0, x1, y0, y1], origin='lower', aspect='auto',
                               cmap=cmap, zorder=1, interpolation='bilinear')
                im.set_clip_path(patch)
        for s in segs:
            glow(ax, s[:, 0], s[:, 1], '#ffd6e0' if heart else BLUE)
        ax.set_xlim(x0, x1)
        ax.set_ylim(y0, y1)
        ax.set_aspect('equal', adjustable='datalim')
    else:
        fail('Unknown graph type')

    title = d.get('title', '')
    if title:
        t = None
        try:
            t = fig.text(0.5, 0.905, '$' + title + '$', ha='center', va='center', fontsize=26, color='white')
            fig.canvas.draw()
            bb = t.get_window_extent(fig.canvas.get_renderer())
            fw = fig.get_figwidth() * fig.dpi * 0.94
            fh = fig.get_figheight() * fig.dpi * 0.13
            k = min(fw / bb.width, fh / bb.height, 1.0)
            if k < 1.0:
                t.set_fontsize(max(7, 26 * k))
                fig.canvas.draw()
        except Exception:
            if t is not None:
                try:
                    t.remove()
                except Exception:
                    pass
    fig.savefig(out, dpi=160, facecolor=BG)


main()
