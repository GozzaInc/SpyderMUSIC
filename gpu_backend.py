"""Fixed GPU rendering backend for SpyderMUSIC V4.

The backdrop and compatible theme visuals are painted through one OpenGL
surface. QWidget / QLabel continue to own alpha-sensitive foreground, input,
cover masks and RMS seekbars, preserving the original theme z order.
"""
from PySide6.QtOpenGLWidgets import QOpenGLWidget

def paint_source_cover(painter, destination, pixmap):
    """Aspect-fill from original pixmap, cropping in source coordinates.

    Avoid QPixmap.scaled() in paint callbacks: the OpenGL paint engine can
    stretch/crop the original image texture instead of resampling on the CPU.
    """
    from PySide6.QtCore import QRectF
    if pixmap is None or pixmap.isNull():
        return
    target = QRectF(destination)
    sw, sh = pixmap.width(), pixmap.height()
    if sw <= 0 or sh <= 0 or target.width() <= 0 or target.height() <= 0:
        return
    scale = max(target.width() / sw, target.height() / sh)
    srcw, srch = target.width() / scale, target.height() / scale
    source = QRectF((sw-srcw)/2.0, (sh-srch)/2.0, srcw, srch)
    painter.drawPixmap(target, pixmap, source)


def clear_gpu_surface(widget):
    """Clear the current OpenGL framebuffer to transparent RGBA before painting.

    QOpenGLWidget does not clear after a paintGL() override; without this,
    partially transparent themed layers can retain previous frame pixels.
    """
    context = widget.context()
    if context is not None:
        functions = context.functions()
        functions.glClearColor(0.0, 0.0, 0.0, 0.0)
        functions.glClear(0x00004000)  # GL_COLOR_BUFFER_BIT
