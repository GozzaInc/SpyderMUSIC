"""SpyderMUSIC compact multi-shape PEQ lab.
Requires PySide6, numpy and libmpv Windows DLL.
Usage: python spyder_peq_compact_filters.py --dll path/to/libmpv_windows-x86_64.dll
Optional: --audio song.flac
"""
"""Standalone PySide6 10-band PEQ laboratory for SpyderMUSIC's libmpv.

Run: python spyder_peq_qt_bell_clean_values.py --dll path/to/libmpv_windows-x86_64.dll [--audio song.flac]
Requires: pip install PySide6 numpy
No imports from SpyderMUSIC. PEQController and ResponsePlot can later be reused.
"""
import argparse
import ctypes as C
import json
import math
from pathlib import Path
from PySide6.QtCore import Qt, QPointF, QTimer, Signal, QSignalBlocker, Property
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen, QFont
from PySide6.QtWidgets import (QApplication, QCheckBox, QDoubleSpinBox, QFileDialog,
                               QFrame, QGridLayout, QHBoxLayout, QLabel, QMainWindow, QComboBox, QMenu,
                               QMessageBox, QPushButton, QScrollArea, QVBoxLayout, QWidget)

FREQS = (31, 62, 125, 250, 500, 1000, 2000, 4000, 8000, 16000)
COLORS = ('#00c9db','#ffae42','#bf83ff','#91d26c','#ff78aa','#ffe070','#4fd6ba','#ff765c','#899fff','#b5df91')

class MPV:
    def __init__(self, dll):
        self.lib = C.CDLL(str(Path(dll).resolve()))
        for name, restype, args in (
            ('mpv_create', C.c_void_p, []), ('mpv_initialize', C.c_int, [C.c_void_p]),
            ('mpv_terminate_destroy', None, [C.c_void_p]),
            ('mpv_set_option_string', C.c_int, [C.c_void_p,C.c_char_p,C.c_char_p]),
            ('mpv_command', C.c_int, [C.c_void_p,C.POINTER(C.c_char_p)]),
            ('mpv_error_string', C.c_char_p, [C.c_int])):
            fn = getattr(self.lib, name); fn.restype = restype; fn.argtypes = args
        self.handle = self.lib.mpv_create()
        if not self.handle: raise RuntimeError('mpv_create failed')
        try:
            for k,v in [('vid','no'),('audio-display','no'),('terminal','no'),('idle','yes'),('volume','70')]:
                self.check(self.lib.mpv_set_option_string(self.handle,k.encode(),v.encode()),k)
            self.check(self.lib.mpv_initialize(self.handle),'initialize')
            self.command('af','add','@spydervu:lavfi=[astats=metadata=1:reset=1]')
        except Exception:
            self.close(); raise
    def check(self, rc, what):
        if rc < 0:
            err=self.lib.mpv_error_string(rc)
            raise RuntimeError(f'{what}: {err.decode(errors="replace") if err else rc}')
    def command(self,*args):
        argv=(C.c_char_p*(len(args)+1))(*(str(a).encode('utf-8') for a in args),None)
        self.check(self.lib.mpv_command(self.handle,argv),' '.join(map(str,args)))
    def close(self):
        if self.handle:
            self.lib.mpv_terminate_destroy(self.handle); self.handle=None


import numpy as np

FILTER_TYPES = ('Bell', 'Low Shelf', 'High Shelf', 'Notch', 'Low Pass', 'High Pass')
def clamp(v, a, b): return max(a, min(b, v))

def filter_spec(b):
    f=clamp(float(b['freq']),20,20000)
    q=clamp(float(b['q']),.1,20)
    g=clamp(float(b['gain']),-18,18)
    t=b['type']
    if t=='Bell': return f'equalizer=f={f:.4f}:t=q:w={q:.4f}:g={g:.4f}:r=f64'
    if t=='Low Shelf': return f'lowshelf=f={f:.4f}:t=q:w={q:.4f}:g={g:.4f}:r=f64'
    if t=='High Shelf': return f'highshelf=f={f:.4f}:t=q:w={q:.4f}:g={g:.4f}:r=f64'
    if t=='Notch': return f'bandreject=f={f:.4f}:t=q:w={q:.4f}:r=f64'
    if t=='Low Pass': return f'lowpass=f={f:.4f}:t=q:w={q:.4f}:p=2:r=f64'
    if t=='High Pass': return f'highpass=f={f:.4f}:t=q:w={q:.4f}:p=2:r=f64'
    raise ValueError(t)

def response_db(b, freqs, sr=96000):
    """Digital biquad transfer function. Bell matches FFmpeg's equalizer.
    Other shapes are RBJ-style predictions, pending exact per-filter verification.
    """
    f=np.asarray(freqs,dtype=float)
    fc=clamp(float(b['freq']),20,min(20000,sr*.49))
    q=clamp(float(b['q']),.1,20)
    gain=float(b['gain'])
    t=b['type']
    if t in ('Bell','Low Shelf','High Shelf') and abs(gain)<1e-9:
        return np.zeros_like(f)
    w0=2*np.pi*fc/sr
    cs=np.cos(w0); sn=np.sin(w0); alpha=sn/(2*q)
    A=10**(gain/40)
    if t=='Bell':
        co=(1+alpha*A,-2*cs,1-alpha*A,1+alpha/A,-2*cs,1-alpha/A)
    elif t=='Low Shelf':
        beta=2*np.sqrt(A)*alpha
        co=(A*((A+1)-(A-1)*cs+beta),2*A*((A-1)-(A+1)*cs),A*((A+1)-(A-1)*cs-beta),
            (A+1)+(A-1)*cs+beta,-2*((A-1)+(A+1)*cs),(A+1)+(A-1)*cs-beta)
    elif t=='High Shelf':
        beta=2*np.sqrt(A)*alpha
        co=(A*((A+1)+(A-1)*cs+beta),-2*A*((A-1)+(A+1)*cs),A*((A+1)+(A-1)*cs-beta),
            (A+1)-(A-1)*cs+beta,2*((A-1)-(A+1)*cs),(A+1)-(A-1)*cs-beta)
    elif t=='Notch':
        co=(1,-2*cs,1,1+alpha,-2*cs,1-alpha)
    elif t=='Low Pass':
        co=((1-cs)/2,1-cs,(1-cs)/2,1+alpha,-2*cs,1-alpha)
    elif t=='High Pass':
        co=((1+cs)/2,-(1+cs),(1+cs)/2,1+alpha,-2*cs,1-alpha)
    else: return np.zeros_like(f)
    b0,b1,b2,a0,a1,a2=co
    z=np.exp(-2j*np.pi*f/sr)
    h=(b0+b1*z+b2*z*z)/(a0+a1*z+a2*z*z)
    return 20*np.log10(np.maximum(1e-12,np.abs(h)))

class PEQController:
    LABEL='@spydereq'
    def __init__(self,decks):
        self.decks=list(decks)
        self.bands=[dict(freq=float(f),gain=0.,q=1.,enabled=True,type='Bell') for f in FREQS]
        self.preamp=-3.;self.enabled=True
        self.installed=[False]*len(self.decks)
    def expression(self):
        return ','.join(filter_spec(b) for b in self.bands if b['enabled'] and
            (b['type'] not in ('Bell','Low Shelf','High Shelf') or abs(b['gain'])>1e-6))
    def apply(self):
        expr=self.expression()
        chain=(f'{expr},' if expr else '')+f'volume={10**(self.preamp/20):.9f}'
        spec=f'{self.LABEL}:lavfi=[{chain}]' if self.enabled else None
        for i,deck in enumerate(self.decks):
            if self.installed[i]:
                deck.command('af','remove',self.LABEL)
                self.installed[i]=False
            if spec:
                try:
                    deck.command('af','add',spec)
                    self.installed[i]=True
                except Exception:
                    # Remove any partial graph; never leave a broken EQ in the playback path.
                    try: deck.command('af','remove',self.LABEL)
                    except Exception: pass
                    raise
    def save(self,path):
        Path(path).write_text(json.dumps(dict(bands=self.bands,preamp=self.preamp,enabled=self.enabled),indent=2),encoding='utf-8')
    def load(self,path):
        obj=json.loads(Path(path).read_text(encoding='utf-8'))
        if len(obj['bands'])!=10:raise ValueError('Exactly 10 bands required')
        bands=[]
        for b in obj['bands']:
            f=float(b['freq']);g=float(b['gain'])
            q=float(b.get('q',f/float(b['bandwidth']) if 'bandwidth' in b else 1))
            t=b.get('type','Bell')
            if t not in FILTER_TYPES or not (20<=f<=20000 and -18<=g<=18 and .1<=q<=20):
                raise ValueError('Invalid band')
            bands.append(dict(freq=f,gain=g,q=q,type=t,enabled=bool(b['enabled'])))
        p=float(obj['preamp'])
        if not -24<=p<=12:raise ValueError('Invalid preamp')
        self.bands=bands;self.preamp=p;self.enabled=bool(obj['enabled'])

class ResponsePlot(QWidget):
    bandMoved=Signal(int,float,float)
    bandQWheel=Signal(int,int,bool)
    bandReset=Signal(int)
    bandSelected=Signal(int)
    preampWheel=Signal(int,bool)
    eqToggled=Signal(bool)
    def __init__(self):
        super().__init__()
        self.bands=[];self.preamp=0;self.active=True;self.individual=True
        self.setObjectName('peq_plot')
        self._peq_colors = {
            'Background': QColor('#10141c'), 'Grid': QColor('#293543'),
            'Axis': QColor('#b9c8d7'), 'Sum': QColor('#ffffff'),
            **{f'Band{i+1}': QColor(c) for i,c in enumerate(COLORS)}
        }
        self.selected=0;self.hovered=None;self.dragging=None;self.sr=96000
        self.edit_locked=False
        self.setMinimumHeight(140);self.setMouseTracking(True)
        self.eq_button=QPushButton('EQ ON',self);self.eq_button.setCheckable(True)
        self.eq_button.setFixedSize(68,25)
        self.eq_button.setStyleSheet('QPushButton {background:#27323f;color:white;border:1px solid #6e8495;border-radius:4px;} QPushButton:checked {background:#195d46;border-color:#58d5a6;}')
        self.eq_button.toggled.connect(self.eqToggled.emit);self.eq_button.hide()
    def resizeEvent(self,event):
        self.eq_button.move(self.width()-78,8)
        super().resizeEvent(event)
    def set_eq_enabled(self,flag):
        with QSignalBlocker(self.eq_button):self.eq_button.setChecked(flag)
        self.eq_button.setText('EQ ON' if flag else 'EQ OFF')
    def enterEvent(self,event):
        self.eq_button.show();super().enterEvent(event)
    def leaveEvent(self,event):
        self.eq_button.hide();self.hovered=None;self.update();super().leaveEvent(event)
    def bounds(self):return 49,self.width()-20,16,self.height()-29
    def xp(self,f):
        l,r,_,_=self.bounds()
        return l+np.log10(clamp(f,20,20000)/20)/3*(r-l)
    def yp(self,g):
        _,_,t,b=self.bounds()
        return t+(24-clamp(g,-24,24))/48*(b-t)
    def point_values(self,pos):
        l,r,t,b=self.bounds()
        f=20*1000**((clamp(pos.x(),l,r)-l)/max(1,r-l))
        g=24-48*(clamp(pos.y(),t,b)-t)/max(1,b-t)
        return clamp(f,20,20000),clamp(g,-18,18)
    def set_data(self,bands,preamp,active):
        self.bands=[dict(b) for b in bands];self.preamp=preamp;self.active=active;self.update()
    def nearest(self,pos):
        idx=None;dist=18**2
        for i,b in enumerate(self.bands):
            if not b['enabled']:continue
            y=b['gain'] if b['type'] in ('Bell','Low Shelf','High Shelf') else 0
            d=(pos.x()-self.xp(b['freq']))**2+(pos.y()-self.yp(y))**2
            if d<dist:dist=d;idx=i
        return idx
    def mousePressEvent(self,event):
        if self.edit_locked:
            event.ignore();return
        if event.button()==Qt.LeftButton:
            idx=self.nearest(event.position())
            if idx is not None:
                self.selected=idx;self.bandSelected.emit(idx);self.dragging=idx;self.update();event.accept();return
        super().mousePressEvent(event)
    def mouseDoubleClickEvent(self,event):
        if self.edit_locked:
            event.ignore();return
        idx=self.nearest(event.position())
        if idx is not None and event.button()==Qt.LeftButton:self.bandReset.emit(idx);event.accept();return
        super().mouseDoubleClickEvent(event)
    def mouseMoveEvent(self,event):
        if self.edit_locked:
            self.setCursor(Qt.ArrowCursor);event.ignore();return
        if self.dragging is not None:
            f,g=self.point_values(event.position())
            self.bandMoved.emit(self.dragging,f,g)
            event.accept();return
        self.hovered=self.nearest(event.position())
        self.setCursor(Qt.OpenHandCursor if self.hovered is not None else Qt.ArrowCursor)
        self.update();super().mouseMoveEvent(event)
    def mouseReleaseEvent(self,event):
        if self.dragging is not None:self.dragging=None;self.update();event.accept();return
        super().mouseReleaseEvent(event)
    def wheelEvent(self,event):
        if self.edit_locked:
            event.ignore();return
        d=event.angleDelta().y()
        if not d:return
        idx=self.nearest(event.position())
        fine=bool(event.modifiers()&Qt.ControlModifier)
        if idx is None:self.preampWheel.emit(d,fine)
        else:self.bandQWheel.emit(idx,d,fine)
        event.accept()
    def paintEvent(self,event):
        p=QPainter(self);p.setRenderHint(QPainter.Antialiasing)
        p.fillRect(self.rect(),self._peq_colors['Background'])
        l,r,t,bot=self.bounds()
        if r-l<90 or bot-t<60:return
        p.setFont(QFont('Segoe UI',8))
        for db in (-24,-12,0,12,24):
            p.setPen(QPen((self._peq_colors['Axis'] if db==0 else self._peq_colors['Grid']),1))
            p.drawLine(QPointF(l,self.yp(db)),QPointF(r,self.yp(db)))
            p.setPen(self._peq_colors['Axis']);p.drawText(3,int(self.yp(db)+4),str(db))
        for f in (20,50,100,200,500,1000,2000,5000,10000,20000):
            p.setPen(QPen(self._peq_colors['Grid'],1,Qt.DotLine))
            p.drawLine(QPointF(self.xp(f),t),QPointF(self.xp(f),bot))
            p.setPen(self._peq_colors['Axis'])
            p.drawText(int(self.xp(f)-9),int(bot+16),f'{f//1000}k' if f>=1000 else str(f))
        freqs=np.geomspace(20,20000,450)
        total=np.full_like(freqs,self.preamp)
        for i,b in enumerate(self.bands):
            if not b['enabled']:continue
            if b['type'] in ('Bell','Low Shelf','High Shelf') and abs(b['gain'])<1e-7:continue
            vals=response_db(b,freqs,self.sr)
            total+=vals
            if self.individual:
                path=QPainterPath()
                for j,(f,v) in enumerate(zip(freqs,vals)):
                    pt=QPointF(self.xp(float(f)),self.yp(float(v)))
                    if j==0:path.moveTo(pt)
                    else:path.lineTo(pt)
                p.setPen(QPen(self._peq_colors[f'Band{i+1}'],2 if i==self.selected else 1.3))
                p.drawPath(path)
        path=QPainterPath()
        for j,(f,v) in enumerate(zip(freqs,total)):
            pt=QPointF(self.xp(float(f)),self.yp(float(v)))
            if j==0:path.moveTo(pt)
            else:path.lineTo(pt)
        p.setPen(QPen(self._peq_colors['Sum'],2.2));p.drawPath(path)
        for i,b in enumerate(self.bands):
            if not b['enabled']:continue
            y=b['gain'] if b['type'] in ('Bell','Low Shelf','High Shelf') else 0
            x,y=self.xp(b['freq']),self.yp(y)
            radius=8 if i==self.selected else 5
            p.setPen(QPen(self._peq_colors['Sum'],2 if i==self.selected else 1))
            p.setBrush(self._peq_colors[f'Band{i+1}']);p.drawEllipse(QPointF(x,y),radius,radius)
            p.setPen(self._peq_colors['Background']);p.drawText(int(x-11),int(y-5),22,10,Qt.AlignCenter,str(i+1))
        p.end()

    def getPeqBackground(self):
        return self._peq_colors['Background']
    def setPeqBackground(self, color):
        self._peq_colors['Background'] = QColor(color)
        self.update()
    peqBackground = Property(QColor, getPeqBackground, setPeqBackground)
    def getPeqGrid(self):
        return self._peq_colors['Grid']
    def setPeqGrid(self, color):
        self._peq_colors['Grid'] = QColor(color)
        self.update()
    peqGrid = Property(QColor, getPeqGrid, setPeqGrid)
    def getPeqAxis(self):
        return self._peq_colors['Axis']
    def setPeqAxis(self, color):
        self._peq_colors['Axis'] = QColor(color)
        self.update()
    peqAxis = Property(QColor, getPeqAxis, setPeqAxis)
    def getPeqSum(self):
        return self._peq_colors['Sum']
    def setPeqSum(self, color):
        self._peq_colors['Sum'] = QColor(color)
        self.update()
    peqSum = Property(QColor, getPeqSum, setPeqSum)
    def getPeqBand1(self):
        return self._peq_colors['Band1']
    def setPeqBand1(self, color):
        self._peq_colors['Band1'] = QColor(color)
        self.update()
    peqBand1 = Property(QColor, getPeqBand1, setPeqBand1)
    def getPeqBand2(self):
        return self._peq_colors['Band2']
    def setPeqBand2(self, color):
        self._peq_colors['Band2'] = QColor(color)
        self.update()
    peqBand2 = Property(QColor, getPeqBand2, setPeqBand2)
    def getPeqBand3(self):
        return self._peq_colors['Band3']
    def setPeqBand3(self, color):
        self._peq_colors['Band3'] = QColor(color)
        self.update()
    peqBand3 = Property(QColor, getPeqBand3, setPeqBand3)
    def getPeqBand4(self):
        return self._peq_colors['Band4']
    def setPeqBand4(self, color):
        self._peq_colors['Band4'] = QColor(color)
        self.update()
    peqBand4 = Property(QColor, getPeqBand4, setPeqBand4)
    def getPeqBand5(self):
        return self._peq_colors['Band5']
    def setPeqBand5(self, color):
        self._peq_colors['Band5'] = QColor(color)
        self.update()
    peqBand5 = Property(QColor, getPeqBand5, setPeqBand5)
    def getPeqBand6(self):
        return self._peq_colors['Band6']
    def setPeqBand6(self, color):
        self._peq_colors['Band6'] = QColor(color)
        self.update()
    peqBand6 = Property(QColor, getPeqBand6, setPeqBand6)
    def getPeqBand7(self):
        return self._peq_colors['Band7']
    def setPeqBand7(self, color):
        self._peq_colors['Band7'] = QColor(color)
        self.update()
    peqBand7 = Property(QColor, getPeqBand7, setPeqBand7)
    def getPeqBand8(self):
        return self._peq_colors['Band8']
    def setPeqBand8(self, color):
        self._peq_colors['Band8'] = QColor(color)
        self.update()
    peqBand8 = Property(QColor, getPeqBand8, setPeqBand8)
    def getPeqBand9(self):
        return self._peq_colors['Band9']
    def setPeqBand9(self, color):
        self._peq_colors['Band9'] = QColor(color)
        self.update()
    peqBand9 = Property(QColor, getPeqBand9, setPeqBand9)
    def getPeqBand10(self):
        return self._peq_colors['Band10']
    def setPeqBand10(self, color):
        self._peq_colors['Band10'] = QColor(color)
        self.update()
    peqBand10 = Property(QColor, getPeqBand10, setPeqBand10)

class Window(QMainWindow):
    def __init__(self,mpv,audio=None):
        super().__init__()
        self.mpv=mpv;self.peq=PEQController([mpv])
        self.setObjectName('peq_lab_standalone')
        self.setWindowTitle('SpyderMUSIC · Compact PEQ Lab')
        self.resize(850,470);self.display_mode='max';self.max_geometry=None
        self.selected=0;self.updating=False
        self.timer=QTimer(self);self.timer.setSingleShot(True);self.timer.setInterval(300)
        self.timer.timeout.connect(self.apply)
        host=QWidget();self.setCentralWidget(host)
        main=QVBoxLayout(host);main.setContentsMargins(7,6,7,6);main.setSpacing(5)
        self.plot=ResponsePlot();main.addWidget(self.plot,1)
        self.plot.bandMoved.connect(self.move_band)
        self.plot.bandQWheel.connect(self.wheel_q)
        self.plot.bandReset.connect(self.reset_band)
        self.plot.bandSelected.connect(self.select_band)
        self.plot.preampWheel.connect(self.wheel_preamp)
        self.plot.eqToggled.connect(self.set_eq_enabled)
        self.editor=QWidget();main.addWidget(self.editor)
        edit=QHBoxLayout(self.editor);edit.setContentsMargins(3,3,3,3);edit.setSpacing(8)
        band_cell=QWidget()
        band_layout=QVBoxLayout(band_cell)
        band_layout.setContentsMargins(0,0,0,0)
        band_layout.setSpacing(2)
        # Band selection must remain possible even when its plot handle is disabled.
        self.band_selector=QComboBox()
        self.band_selector.setObjectName('peq_band_selector')
        self.band_selector.addItems([f'Band {i+1}' for i in range(10)])
        self.band_selector.currentIndexChanged.connect(self.select_band)
        self.band_selector.setMaxVisibleItems(10)
        self.enabled=QCheckBox('Enabled')
        self.enabled.toggled.connect(self.editor_changed)
        band_layout.addWidget(self.enabled)
        band_layout.addWidget(self.band_selector)
        self.type=QComboBox()
        self.type.addItems(FILTER_TYPES)
        self.type.currentTextChanged.connect(self.editor_changed)
        band_layout.addWidget(self.type)
        edit.addWidget(band_cell,1)
        self.freq=self.make_spin(20,20000,1,' Hz');self.gain=self.make_spin(-18,18,2,' dB')
        self.q=self.make_spin(.1,20,2,'');self.preamp=self.make_spin(-24,12,2,' dB')
        # Keep combo boxes naturally sized; fixed-height hacks can distort popup metrics.
        # Limit popup viewport height independently from the closed combo's height.
        for combo in (self.band_selector, self.type):
            combo.setMaxVisibleItems(10)
            combo.view().setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        for label,spin in [('Frequency',self.freq),('Gain',self.gain),('Q factor',self.q),('Preamp',self.preamp)]:
            cell=QWidget();lay=QVBoxLayout(cell);lay.setContentsMargins(0,0,0,0);lay.setSpacing(2)
            lay.addWidget(QLabel(label));lay.addWidget(spin);edit.addWidget(cell,1)
        self.freq.valueChanged.connect(self.editor_changed)
        self.gain.valueChanged.connect(self.editor_changed)
        self.q.valueChanged.connect(self.editor_changed)
        self.preamp.valueChanged.connect(self.editor_changed)
        self.menu=QMenu(self)
        self.lock_action=self.menu.addAction("🔒 Lock PEQ controls")
        self.lock_action.setCheckable(True)
        self.lock_action.toggled.connect(self.set_edit_locked)
        self.menu.addSeparator()
        self.menu.addAction('Flat (reset gains)',self.flat)
        self.menu.addAction('Load preset…',self.load)
        self.menu.addAction('Save preset…',self.save)
        self.show_action=self.menu.addAction('Show individual curves')
        self.show_action.setCheckable(True);self.show_action.setChecked(True)
        self.show_action.toggled.connect(self.curves_changed)
        self.menu.addSeparator()
        self.mode_action=self.menu.addAction('Mini mode',self.toggle_mode)
        self.menu_button=QPushButton('⋯',self.plot)
        self.menu_button.setFixedSize(31,25)
        self.menu_button.setStyleSheet('QPushButton {background:#27323f;color:white;border:1px solid #6e8495;border-radius:4px;}')
        self.menu_button.clicked.connect(lambda:self.menu.exec(self.menu_button.mapToGlobal(self.menu_button.rect().bottomLeft())))
        self.plot.installEventFilter(self)
        self.menu_button.hide()
        self.status=QLabel('');self.status.hide();main.addWidget(self.status)
        self.setStyleSheet(
            'QMainWindow#peq_lab_standalone {background:#20242b;color:#e4eaf2;}'
            'QDoubleSpinBox,QComboBox,QPushButton {color:#e4eaf2;background:#303845;'
            'border:1px solid #526072;border-radius:4px;padding:4px;}'
            'QDoubleSpinBox::up-button,QDoubleSpinBox::down-button {width:0px;height:0px;border:0px;}'
            'QDoubleSpinBox::up-arrow,QDoubleSpinBox::down-arrow {width:0px;height:0px;}'
            'QComboBox::drop-down {width:16px;border:0px;}')
        self.plot.set_eq_enabled(True)
        self.select_band(0)
        self.changed()
        if audio:self.play(audio)
    def eventFilter(self,obj,event):
        if obj is self.plot:
            if event.type()==event.Type.Resize:
                self.menu_button.move(max(4,self.plot.width()-116),8)
            elif event.type()==event.Type.Enter:self.menu_button.show()
            elif event.type()==event.Type.Leave:self.menu_button.hide()
        return super().eventFilter(obj,event)
    def make_spin(self,lo,hi,decimals,suffix):
        s=QDoubleSpinBox();s.setRange(lo,hi);s.setDecimals(decimals)
        s.setButtonSymbols(QDoubleSpinBox.NoButtons)
        s.setSuffix(suffix);s.setMinimumWidth(70);s.setKeyboardTracking(False)
        return s
    def select_band(self,i):
        if not 0 <= i < len(self.peq.bands):
            return
        self.selected=i;self.plot.selected=i
        b=self.peq.bands[i];self.updating=True
        with QSignalBlocker(self.band_selector):
            self.band_selector.setCurrentIndex(i)
        self.enabled.setChecked(b['enabled'])
        self.type.setCurrentText(b['type'])
        self.freq.setValue(b['freq']);self.gain.setValue(b['gain'])
        self.q.setValue(b['q']);self.preamp.setValue(self.peq.preamp)
        self.gain.setEnabled(b['type'] in ('Bell','Low Shelf','High Shelf'))
        self.updating=False;self.plot.update()
    def editor_changed(self,*_):
        if self.updating or self.plot.edit_locked:return
        b=self.peq.bands[self.selected]
        b.update(enabled=self.enabled.isChecked(),type=self.type.currentText(),
                 freq=self.freq.value(),gain=self.gain.value(),q=self.q.value())
        self.gain.setEnabled(b['type'] in ('Bell','Low Shelf','High Shelf'))
        self.peq.preamp=self.preamp.value();self.changed()
    def changed(self):
        self.plot.set_data(self.peq.bands,self.peq.preamp,self.peq.enabled)
        self.timer.start()
    def set_eq_enabled(self,enabled):
        self.peq.enabled=enabled;self.plot.set_eq_enabled(enabled);self.changed()
    def move_band(self,i,f,g):
        if self.plot.edit_locked:return
        b=self.peq.bands[i];b['freq']=round(f,1)
        if b['type'] in ('Bell','Low Shelf','High Shelf'):b['gain']=round(g,2)
        if i==self.selected:self.select_band(i)
        self.changed()
    def wheel_q(self,i,delta,fine):
        if self.plot.edit_locked:return
        b=self.peq.bands[i]
        b['q']=round(clamp(b['q']*(1.01 if fine else 1.08)**(delta/120),.1,20),2)
        if i==self.selected:self.select_band(i)
        self.changed()
    def wheel_preamp(self,delta,fine):
        if self.plot.edit_locked:return
        self.peq.preamp=round(clamp(self.peq.preamp+delta/120*(.1 if fine else .3),-24,12),2)
        self.select_band(self.selected);self.changed()
    def reset_band(self,i):
        if self.plot.edit_locked:return
        if self.peq.bands[i]['type'] in ('Notch','Low Pass','High Pass'):
            self.peq.bands[i]['enabled']=False
        else:
            self.peq.bands[i]['gain']=0
        if i==self.selected:self.select_band(i)
        self.changed()
    def set_edit_locked(self,locked):
        """Lock parameter editing, not DSP enable/disable or playback."""
        self.plot.edit_locked=bool(locked)
        self.plot.dragging=None
        for widget in (self.enabled,self.band_selector,self.type,self.freq,self.gain,self.q,self.preamp):
            widget.setEnabled(not locked)
        self.plot.setCursor(Qt.ArrowCursor)
        self.plot.update()
        self.lock_action.setText('🔓 Unlock PEQ controls' if locked else '🔒 Lock PEQ controls')
        for action in (self.menu.actions()[2], self.menu.actions()[3]):
            action.setEnabled(not locked)

    def curves_changed(self,flag):self.plot.individual=flag;self.plot.update()
    def flat(self):
        if self.plot.edit_locked:return
        for b in self.peq.bands:
            b['gain']=0
            if b['type'] in ('Notch','Low Pass','High Pass'):b['enabled']=False
        self.select_band(self.selected);self.changed()
    def toggle_mode(self):
        if self.display_mode=='max':
            self.max_geometry=self.geometry();self.display_mode='mini'
            self.editor.hide();self.mode_action.setText('Max mode')
            self.resize(460,250);self.setMinimumSize(300,160)
        else:
            self.display_mode='max';self.editor.show();self.mode_action.setText('Mini mode')
            self.setMinimumSize(0,0)
            if self.max_geometry:self.setGeometry(self.max_geometry)
        self.plot.update()
    def apply(self):
        try:
            self.peq.apply()
            self.status.hide()
        except Exception as exc:
            self.status.setText(f'Filter error: {exc}');self.status.show()
    def save(self):
        self.timer.stop();self.apply()
        path,_=QFileDialog.getSaveFileName(self,'Save preset','','JSON (*.json)')
        if path:
            if not path.lower().endswith('.json'):path+='.json'
            try:self.peq.save(path)
            except Exception as exc:self.status.setText(str(exc));self.status.show()
    def load(self):
        if self.plot.edit_locked:return
        path,_=QFileDialog.getOpenFileName(self,'Load preset','','JSON (*.json)')
        if not path:return
        try:self.peq.load(path)
        except Exception as exc:self.status.setText(str(exc));self.status.show();return
        self.plot.set_eq_enabled(self.peq.enabled);self.select_band(self.selected);self.changed()
    def play(self,path):
        try:self.mpv.command('loadfile',str(path),'replace')
        except Exception as exc:self.status.setText(str(exc));self.status.show()
    def closeEvent(self,event):
        self.timer.stop();self.mpv.close();event.accept()

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dll',required=True);parser.add_argument('--audio')
    args=parser.parse_args()
    app=QApplication([])
    try:mpv=MPV(args.dll)
    except Exception as exc:
        QMessageBox.critical(None,'libmpv initialization failed',str(exc));return 1
    window=Window(mpv,args.audio);window.show();return app.exec()

if __name__=='__main__':raise SystemExit(main())
