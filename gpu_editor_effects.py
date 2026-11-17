"""GPU-only Glow/Shadow for composited theme visualizations.

The Qt theme schema is untouched.  Each eligible source is painted into an
alpha-only, reduced-resolution OpenGL framebuffer; two separable blur shaders
create its halo.  The original visualization is still painted with its normal
QPainter path on top, so buttons, hit-testing and editor properties are intact.

This module is used solely inside CoverBackdrop.paintGL() with its GL context
current.  Native foreground widgets keep the legacy Qt graphics effect.
"""
import math
import struct

from PySide6.QtCore import QSize
from PySide6.QtGui import QColor, QPainter, QVector2D, QVector4D, QOpenGLContext, QSurfaceFormat
from PySide6.QtOpenGL import (
    QOpenGLBuffer, QOpenGLFramebufferObject, QOpenGLFramebufferObjectFormat,
    QOpenGLPaintDevice, QOpenGLShader, QOpenGLShaderProgram, QOpenGLVertexArrayObject,
)

_GL_COLOR_BUFFER_BIT = 0x00004000
_GL_FRAMEBUFFER = 0x8D40
_GL_TEXTURE_2D = 0x0DE1
_GL_TEXTURE0 = 0x84C0
_GL_TRIANGLES = 0x0004
_GL_FLOAT = 0x1406
_GL_BLEND = 0x0BE2
_GL_ONE = 1
_GL_ONE_MINUS_SRC_ALPHA = 0x0303
_GL_DEPTH_TEST = 0x0B71
_GL_STENCIL_TEST = 0x0B90
_GL_SCISSOR_TEST = 0x0C11
_GL_TEXTURE_MIN_FILTER = 0x2801
_GL_TEXTURE_MAG_FILTER = 0x2800
_GL_LINEAR = 0x2601
_GL_CLAMP_TO_EDGE = 0x812F
_GL_TEXTURE_WRAP_S = 0x2802
_GL_TEXTURE_WRAP_T = 0x2803


# Nine fixed Gaussian taps per axis; spacing follows the 0..100 blur slider.
_VERTEX = """
attribute vec2 a_position;
attribute vec2 a_uv;
varying vec2 v_uv;
void main() { v_uv = a_uv; gl_Position = vec4(a_position, 0.0, 1.0); }
"""
_FRAGMENT = """
varying vec2 v_uv;
uniform sampler2D u_texture;
uniform vec2 u_step;
uniform vec4 u_tint;
uniform float u_colorize;
void main() {
    float alpha = 0.0;
    alpha += texture2D(u_texture, v_uv - 4.0*u_step).a * 0.0276305506;
    alpha += texture2D(u_texture, v_uv - 3.0*u_step).a * 0.0662822453;
    alpha += texture2D(u_texture, v_uv - 2.0*u_step).a * 0.1238315368;
    alpha += texture2D(u_texture, v_uv -       u_step).a * 0.1801738229;
    alpha += texture2D(u_texture, v_uv               ).a * 0.2041636888;
    alpha += texture2D(u_texture, v_uv +       u_step).a * 0.1801738229;
    alpha += texture2D(u_texture, v_uv + 2.0*u_step).a * 0.1238315368;
    alpha += texture2D(u_texture, v_uv + 3.0*u_step).a * 0.0662822453;
    alpha += texture2D(u_texture, v_uv + 4.0*u_step).a * 0.0276305506;
    // Premultiplied RGBA is what QPainter's GL composition expects.
    vec4 tint = mix(vec4(1.0), u_tint, u_colorize);
    float a = clamp(alpha * tint.a, 0.0, 1.0);
    gl_FragColor = vec4(tint.rgb * a, a);
}
"""


def _sources(context):
    """Choose a shader dialect compatible with the current Qt GL context."""
    fmt = context.format()
    if context.isOpenGLES():
        version = '#version 300 es\n' if fmt.majorVersion() >= 3 else ''
        if fmt.majorVersion() >= 3:
            vs = _VERTEX.replace('attribute', 'in').replace('varying', 'out')
            fs = _FRAGMENT.replace('varying', 'in').replace('texture2D', 'texture').replace(
                'gl_FragColor', 'fragColor')
            fs = 'out vec4 fragColor;\n' + fs
        else:
            vs, fs = _VERTEX, _FRAGMENT
        return (version + 'precision highp float;\n' + vs,
                version + 'precision mediump float;\n' + fs)
    if fmt.profile() == QSurfaceFormat.OpenGLContextProfile.CoreProfile:
        version = '#version 330 core\n' if (fmt.majorVersion(), fmt.minorVersion()) >= (3, 3) else '#version 150\n'
        vs = _VERTEX.replace('attribute', 'in').replace('varying', 'out')
        fs = ('out vec4 fragColor;\n' + _FRAGMENT.replace('varying', 'in')
              .replace('texture2D', 'texture').replace('gl_FragColor', 'fragColor'))
        return version + vs, version + fs
    return '#version 120\n' + _VERTEX, '#version 120\n' + _FRAGMENT


def _effect_geometry(widget, effect):
    """Unclipped bounds: halos may extend beyond the original widget."""
    radius = max(0.0, min(100.0, float(effect.get('blur', 24))))
    pad = int(math.ceil(radius * 1.5 + 3))
    if effect.get('type') == 'shadow':
        angle = math.radians(float(effect.get('angle', 45)))
        distance = float(effect.get('distance', 12))
        dx, dy = math.cos(angle) * distance, math.sin(angle) * distance
    else:
        dx, dy = 0.0, 0.0
    return pad, dx, dy


class GPUVisualEffectRenderer:
    """Resources are owned by one QOpenGLWidget context, never by a worker."""

    def __init__(self, backdrop):
        self.context = QOpenGLContext.currentContext()
        if self.context is None or backdrop.context() != self.context:
            raise RuntimeError('No current backdrop GL context')
        self.gl = self.context.functions()
        self._targets = {}  # widget -> (key, source_fbo, horizontal_fbo, pad, dx, dy, step)
        vertex, fragment = _sources(self.context)
        self.program = QOpenGLShaderProgram()
        if not self.program.addShaderFromSourceCode(QOpenGLShader.ShaderTypeBit.Vertex, vertex):
            raise RuntimeError('Vertex shader: ' + self.program.log())
        if not self.program.addShaderFromSourceCode(QOpenGLShader.ShaderTypeBit.Fragment, fragment):
            raise RuntimeError('Fragment shader: ' + self.program.log())
        self.program.bindAttributeLocation('a_position', 0)
        self.program.bindAttributeLocation('a_uv', 1)
        if not self.program.link():
            raise RuntimeError('Effect shader link: ' + self.program.log())
        self.vao = QOpenGLVertexArrayObject()
        if not self.vao.create():
            raise RuntimeError('GPU effects require vertex array objects')
        self.vbo = QOpenGLBuffer(QOpenGLBuffer.Type.VertexBuffer)
        if not self.vbo.create():
            raise RuntimeError('GPU effects cannot create vertex buffer')
        self.vao.bind()
        self.vbo.bind()
        self.vbo.allocate(6 * 4 * 4)
        self.program.bind()
        self.program.enableAttributeArray(0)
        self.program.setAttributeBuffer(0, _GL_FLOAT, 0, 2, 4 * 4)
        self.program.enableAttributeArray(1)
        self.program.setAttributeBuffer(1, _GL_FLOAT, 2 * 4, 2, 4 * 4)
        self.program.release()
        self.vbo.release()
        self.vao.release()

    @staticmethod
    def _new_fbo(size, paintable=False):
        fmt = QOpenGLFramebufferObjectFormat()
        if paintable:
            fmt.setAttachment(QOpenGLFramebufferObject.Attachment.CombinedDepthStencil)
        fbo = QOpenGLFramebufferObject(size, fmt)
        if not fbo.isValid():
            raise RuntimeError('Unable to allocate effect FBO %sx%s' % (size.width(), size.height()))
        return fbo

    def _render_quad(self, texture, bounds, viewport, step, tint, colorize):
        """Run the same blur shader to a horizontal FBO or the live backdrop."""
        vx, vy, vw, vh = bounds
        width, height = viewport
        x0 = 2.0 * vx / width - 1.0
        x1 = 2.0 * (vx + vw) / width - 1.0
        y0 = 1.0 - 2.0 * vy / height
        y1 = 1.0 - 2.0 * (vy + vh) / height
        # OpenGL textures have bottom-left origin. Qt paints the FBO image
        # upright through the OpenGL paint engine; map the quad accordingly.
        quad = (x0,y0,0.0,1.0, x0,y1,0.0,0.0, x1,y1,1.0,0.0,
                x0,y0,0.0,1.0, x1,y1,1.0,0.0, x1,y0,1.0,1.0)
        packed = struct.pack('24f', *quad)
        self.program.bind()
        self.vao.bind()
        self.vbo.bind()
        self.vbo.allocate(packed, len(packed))
        self.vbo.release()
        self.program.setUniformValue('u_texture', 0)
        self.program.setUniformValue('u_step', QVector2D(*step))
        self.program.setUniformValue('u_tint', QVector4D(*tint))
        self.program.setUniformValue('u_colorize', float(colorize))
        self.gl.glActiveTexture(_GL_TEXTURE0)
        self.gl.glBindTexture(_GL_TEXTURE_2D, texture)
        self.gl.glTexParameteri(_GL_TEXTURE_2D, _GL_TEXTURE_MIN_FILTER, _GL_LINEAR)
        self.gl.glTexParameteri(_GL_TEXTURE_2D, _GL_TEXTURE_MAG_FILTER, _GL_LINEAR)
        self.gl.glTexParameteri(_GL_TEXTURE_2D, _GL_TEXTURE_WRAP_S, _GL_CLAMP_TO_EDGE)
        self.gl.glTexParameteri(_GL_TEXTURE_2D, _GL_TEXTURE_WRAP_T, _GL_CLAMP_TO_EDGE)
        self.gl.glDrawArrays(_GL_TRIANGLES, 0, 6)
        self.gl.glBindTexture(_GL_TEXTURE_2D, 0)
        self.vao.release()
        self.program.release()

    def prepare(self, backdrop, widgets):
        """Build alpha masks and horizontal passes before the main QPainter."""
        active = {w for w in widgets if (w.spec.get('editor_effect') or {}).get('enabled', False)}
        # Reclaim GPU textures when a theme removes/replaces a visualization.
        for old in list(self._targets):
            if old not in active:
                del self._targets[old]
        try:
            for widget in active:
                fx = widget.spec['editor_effect']
                pad, dx, dy = _effect_geometry(widget, fx)
                logical_w = widget.width() + 2 * pad
                logical_h = widget.height() + 2 * pad
                # Low-resolution masks dramatically reduce both blur passes;
                # this still leaves the actual original visual full-resolution.
                scale = min(0.5, 1536.0 / logical_w, 1536.0 / logical_h)
                fw = max(1, int(math.ceil(logical_w * scale)))
                fh = max(1, int(math.ceil(logical_h * scale)))
                key = (fw, fh, logical_w, logical_h, pad, float(fx.get('blur', 24)))
                cached = self._targets.get(widget)
                if cached is None or cached[0] != key:
                    size = QSize(fw, fh)
                    source = self._new_fbo(size, paintable=True)
                    horizontal = self._new_fbo(size)
                else:
                    source, horizontal = cached[1:3]
                self._targets[widget] = (key, source, horizontal, pad, dx, dy, scale)
                if cached is not None and cached[0] == key and not getattr(widget, '_gpu_effect_source_dirty', True):
                    continue  # static sprites need no offscreen redraw or blur
                source.bind()
                self.gl.glViewport(0, 0, fw, fh)
                self.gl.glClearColor(0.0, 0.0, 0.0, 0.0)
                self.gl.glClear(_GL_COLOR_BUFFER_BIT)
                device = QOpenGLPaintDevice(QSize(fw, fh))
                painter = QPainter(device)
                try:
                    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
                    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
                    painter.scale(scale, scale)
                    painter.translate(pad, pad)
                    widget.paint_into(painter)
                finally:
                    painter.end()
                horizontal.bind()
                self.gl.glViewport(0, 0, fw, fh)
                self.gl.glDisable(_GL_BLEND)
                self.gl.glDisable(_GL_DEPTH_TEST)
                self.gl.glDisable(_GL_STENCIL_TEST)
                self.gl.glDisable(_GL_SCISSOR_TEST)
                self.gl.glClearColor(0.0, 0.0, 0.0, 0.0)
                self.gl.glClear(_GL_COLOR_BUFFER_BIT)
                spacing = max(0.0, float(fx.get('blur', 24)) * scale / 5.0)
                self._render_quad(source.texture(), (0, 0, fw, fh), (fw, fh),
                                  (spacing / fw, 0.0), (1.,1.,1.,1.), 0.0)
                widget._gpu_effect_source_dirty = False
        finally:
            # FBO 0 is *not* QOpenGLWidget's surface. Restore its own FBO!
            self.gl.glBindFramebuffer(_GL_FRAMEBUFFER, backdrop.defaultFramebufferObject())
            dpr = backdrop.devicePixelRatioF()
            self.gl.glViewport(0, 0, int(round(backdrop.width()*dpr)),
                               int(round(backdrop.height()*dpr)))

    def composite(self, widget, painter, backdrop):
        entry = self._targets.get(widget)
        if entry is None:
            return
        _, _, horizontal, pad, dx, dy, scale = entry
        fx = widget.spec['editor_effect']
        color = QColor(fx.get('color', '#000000'))
        opacity = max(0., min(1., float(fx.get('opacity', 65)) / 100.0))
        origin = widget.mapTo(backdrop, widget.rect().topLeft())
        pos_x = float(origin.x()) - pad + dx
        pos_y = float(origin.y()) - pad + dy
        width = float(widget.width() + 2 * pad)
        height = float(widget.height() + 2 * pad)
        bw, bh = max(1, backdrop.width()), max(1, backdrop.height())
        spacing = max(0.0, float(fx.get('blur', 24)) * scale / 5.0)
        painter.beginNativePainting()
        try:
            self.gl.glBindFramebuffer(_GL_FRAMEBUFFER, backdrop.defaultFramebufferObject())
            dpr = backdrop.devicePixelRatioF()
            self.gl.glViewport(0, 0, int(round(bw*dpr)), int(round(bh*dpr)))
            self.gl.glDisable(_GL_DEPTH_TEST)
            self.gl.glDisable(_GL_STENCIL_TEST)
            self.gl.glDisable(_GL_SCISSOR_TEST)
            self.gl.glEnable(_GL_BLEND)
            self.gl.glBlendFunc(_GL_ONE, _GL_ONE_MINUS_SRC_ALPHA)
            self._render_quad(horizontal.texture(),
                              (pos_x, pos_y, width, height), (bw, bh),
                              (0., spacing / max(1, horizontal.size().height())),
                              (color.redF(), color.greenF(), color.blueF(), opacity), 1.0)
        finally:
            painter.endNativePainting()
