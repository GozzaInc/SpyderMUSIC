"""Stage 17.9: visual, non-destructive QSS rule editor."""
import re
from PySide6.QtCore import Qt, QEvent
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (QDialog, QWidget, QVBoxLayout, QHBoxLayout, QFormLayout,
    QLabel, QPushButton, QLineEdit, QComboBox, QCheckBox, QSlider, QProgressBar,
    QTabWidget, QPlainTextEdit, QListWidget, QFrame, QSpinBox, QFontComboBox,
    QColorDialog, QDialogButtonBox, QSplitter, QScrollArea, QGroupBox, QTableWidget,
    QTableWidgetItem, QScrollBar, QTabBar, QAbstractItemView, QRadioButton, QMenu)

RULE = re.compile(r'([^{}]+)\{([^{}]*)\}', re.S)
PROPS = ('background', 'color', 'font-family', 'font-size', 'font-weight',
         'border-color', 'border-width', 'border-style', 'border-radius')

class QssStudio(QDialog):
    def __init__(self, parent, qss, panel_titles=None):
        super().__init__(parent)
        self.setWindowTitle('QSS Studio M2 · Appearance Manager')
        # Compact window size matching the screenshot
        self.resize(1180, 670)
        self._busy = False
        self._original = qss
        self._panel_titles = {k: dict(v) for k, v in (panel_titles or {}).items()}
        self._selected_group = 'tabs'
        self._group_lookup = {}
        self._grouped_choices = {}
        outer = QVBoxLayout(self)
        self.tabs = QTabWidget(self)
        outer.addWidget(self.tabs)
        
        visual = QWidget()
        h = QHBoxLayout(visual)
        self.split = QSplitter(Qt.Horizontal)
        h.addWidget(self.split)
        
        # Linke Spalte: Lebensechte, interaktive Cockpit-Vorschau
        self.preview_scroll = QScrollArea()
        self.preview_scroll.setWidgetResizable(True)
        self.preview_scroll.setMinimumWidth(640)
        self.preview = QWidget()
        self.preview.setObjectName('qssPreviewRoot')
        self.pv = QVBoxLayout(self.preview)
        self.pv.setSpacing(5)
        self.pv.setContentsMargins(8, 8, 8, 8)
        
        self._build_preview()
        self._wire_preview_children()
        self.pv.addStretch(1)
        self.preview_scroll.setWidget(self.preview)
        self.split.addWidget(self.preview_scroll)
        
        # Right column: toolbar and settings
        right = QWidget()
        r = QVBoxLayout(right)
        r.addWidget(QLabel('Selected Area'))
        self.group_title = QLabel('')
        self.group_title.setStyleSheet('font-size: 15px; font-weight: bold; color: #58a6ff; padding: 2px;')
        r.addWidget(self.group_title)
        
        r.addWidget(QLabel('What would you like to style?'))
        self.element_choice = QComboBox()
        r.addWidget(self.element_choice)

        # Widget header content belongs to the theme manifest, not QSS.
        self.title_box = QGroupBox('Panel Title')
        title_form = QFormLayout(self.title_box)
        self.title_text = QLineEdit()
        self.title_visible = QCheckBox('Show title')
        title_form.addRow('Text', self.title_text)
        title_form.addRow('', self.title_visible)
        r.addWidget(self.title_box)
        self.title_text.textEdited.connect(self._title_edited)
        self.title_visible.toggled.connect(self._title_visibility_changed)
        self.title_box.hide()
        
        self.selector = QComboBox()
        self.selector.setEditable(True)
        self.selector.setInsertPolicy(QComboBox.NoInsert)
        self.selector.hide()
        self.part = QComboBox()
        self.part.hide()
        r.addWidget(self.selector)
        r.addWidget(self.part)
        
        self.state_label = QLabel('State (supported states only)')
        r.addWidget(self.state_label)
        self.state = QComboBox()
        r.addWidget(self.state)
        
        self.component_parts = QGroupBox('Select Component')
        # Studio chrome must not inherit the user's QSS component colors.
        self.component_parts.setObjectName('qssStudioComponentParts')
        self.component_parts.setStyleSheet('''
            QGroupBox#qssStudioComponentParts {
                background: transparent; color: #c9d1d9;
                border: 1px solid #30363d; border-radius: 5px;
                margin-top: 9px; padding-top: 7px;
            }
            QGroupBox#qssStudioComponentParts::title {
                subcontrol-origin: margin; left: 9px; padding: 0 4px;
                background: #1c1c1c; color: #c9d1d9;
            }
        ''')
        self.component_parts_layout = QVBoxLayout(self.component_parts)
        self.component_parts_row = QHBoxLayout()
        self.component_parts_layout.addLayout(self.component_parts_row)
        self.component_part_buttons = []
        
        self.slider_height_label = QLabel('Track Height')
        self.slider_height = QSpinBox()
        self.slider_height.setRange(1, 40)
        self.slider_height.setSuffix(' px')
        self.component_parts_layout.addWidget(self.slider_height_label)
        self.component_parts_layout.addWidget(self.slider_height)
        self.slider_height.valueChanged.connect(lambda v: self._write('height', f'{v}px'))
        
        self.handle_width_label = QLabel('Handle Width')
        self.handle_width = QSpinBox()
        self.handle_width.setRange(2, 60)
        self.handle_width.setSuffix(' px')
        self.component_parts_layout.addWidget(self.handle_width_label)
        self.component_parts_layout.addWidget(self.handle_width)
        self.handle_width.valueChanged.connect(lambda v: self._write('width', f'{v}px'))
        r.addWidget(self.component_parts)
        
        self.form = QFormLayout()
        self.fields = {}
        for name, title in [('background','Background'), ('color','Text Color'),
                            ('border-color','Border Color'), ('selection-background-color','Selection Background'), ('selection-color','Selected Text Color')]:
            b = QPushButton('Choose Color…')
            b.clicked.connect(lambda _, n=name: self._choose_color(n))
            self.fields[name] = b
            self.form.addRow(title, b)
            
        self.family = QFontComboBox()
        self.form.addRow('Font Family', self.family)
        self.size = QSpinBox()
        self.size.setRange(0, 120)
        self.size.setSpecialValueText('Inherited')
        self.form.addRow('Font Size (px)', self.size)
        self.bold = QCheckBox('Bold')
        self.form.addRow('Font Style', self.bold)
        
        for name, title in [('border-width','Border Width (px)'),('border-radius','Radius (px)')]:
            s = QSpinBox()
            s.setRange(0, 100)
            self.fields[name] = s
            self.form.addRow(title, s)
        # QColor Q_PROPERTY fields on the custom QPainter PEQ canvas.
        # QSS background-color does not alter the pixels drawn by paintEvent().
        self.peq_color_fields = {}
        for name, label in [('Background', 'Plot Background'),
                            ('Grid', 'Grid Lines'), ('Axis', 'Axes / Labels'),
                            ('Sum', 'Combined Response')] + [
                            (f'Band{i}', f'Band {i} Curve') for i in range(1, 11)]:
            key = 'qproperty-peq' + name
            button = QPushButton('Choose Color…')
            button.clicked.connect(lambda _, k=key: self._choose_color(k))
            self.peq_color_fields[key] = button
            self.form.addRow(label, button)
        r.addLayout(self.form)
        
        self.info = QLabel('Click an element on the left to edit its QSS rule.')
        self.info.setWordWrap(True)
        self.info.setStyleSheet('color: #8b949e; font-size: 11px; padding-top: 4px;')
        r.addWidget(self.info)
        r.addStretch(1)
        
        self.split.addWidget(right)
        self.split.setSizes([670, 390])
        self.tabs.addTab(visual, 'Visual Editor')
        
        self.source = QPlainTextEdit()
        self.source.setPlainText(qss)
        self.tabs.addTab(self.source, 'QSS Source')
        
        self.source.textChanged.connect(self._source_changed)
        self.selector.currentTextChanged.connect(self._base_changed)
        self.element_choice.currentIndexChanged.connect(self._choice_changed)
        self.part.currentIndexChanged.connect(self._selection_changed)
        self.state.currentIndexChanged.connect(self._state_changed)
        self.family.currentFontChanged.connect(lambda f: self._write('font-family', '"' + f.family().replace('"','') + '"'))
        self.size.valueChanged.connect(lambda v: self._write('font-size', f'{v}px') if v else None)
        self.bold.toggled.connect(lambda v: self._write('font-weight', 'bold' if v else 'normal'))
        for key in ('border-width','border-radius'):
            self.fields[key].valueChanged.connect(lambda v, k=key: self._write(k, f'{v}px'))
            
        bar = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bar.accepted.connect(self.accept)
        bar.rejected.connect(self.reject)
        outer.addWidget(bar)
        
        self._source_changed()
        self._choose_group('tabs')

    GROUPS = [
        ('tabs', 'Tab Bar & Menu', [
            ('Tab', 'QTabBar', '::tab'),
            ('Entire Tab Bar', 'QTabBar', ''),
            ('Hamburger Menu Button', 'QPushButton#mainMenuButton', ''),
        ]),
        ('peq', 'Parametric EQ', [
            ('PEQ Panel', 'QMainWindow#peq_panel', ''),
            ('PEQ Plot & Curves', 'QWidget#peq_plot', ''),
            ('PEQ Dropdown', 'QComboBox', ''),
            ('PEQ Number Fields', 'QDoubleSpinBox', ''),
            ('PEQ Buttons', 'QPushButton', ''),
        ]),
        ('brain', 'AI-Log Panel', [
            ('Outer Frame (Container)', 'QFrame#brain_panel', ''),
            ('Title Bar', 'QLabel#brain_header', ''),
            ('Text Area (AI Log)', 'QPlainTextEdit#ai_log', ''),
            ('Scout Bar Frame', 'QFrame#scout_strip', ''),
            ('Scout: Primary Text', 'QWidget[spyderThemeId="scout_primary"]', ''),
            ('Scout: Secondary Text', 'QWidget[spyderThemeId="scout_secondary"]', ''),
        ]),
        ('top', 'Top 100 Panel', [
            ('Outer Frame (Container)', 'QFrame#top_artists_panel', ''),
            ('Title Bar', 'QLabel#top_artists_header', ''),
            ('List', 'QListWidget#top_list', ''),
            ('List Items', 'QListWidget#top_list', '::item'),
        ]),
        ('history', 'Last Songs Panel', [
            ('Outer Frame (Container)', 'QFrame#history_panel', ''),
            ('Title Bar', 'QLabel#history_header', ''),
            ('List', 'QListWidget#history_list', ''),
            ('List Items', 'QListWidget#history_list', '::item'),
        ]),
        ('main', 'Main-Log Panel', [
            ('Outer Frame (Container)', 'QFrame#main_log_panel', ''),
            ('Title Bar / Track Title', 'QLabel#track_title', ''),
            ('Text Area (Main Log)', 'QPlainTextEdit#main_log_text', ''),
            ('VU Meter Frame / Strip', 'QFrame#vu_strip', ''),
        ]),
        ('queue', 'Queue Panel', [
            ('Outer Frame (Container)', 'QFrame#queue_panel', ''),
            ('Title Bar', 'QLabel#queue_header', ''),
            ('Queue List', 'QListWidget#queue_list', ''),
            ('List Items', 'QListWidget#queue_list', '::item'),
            ('Clear Queue Button', 'QPushButton#clear_queue_button', ''),
        ]),
        ('playlist', 'Playlist Panel', [
            ('Playlist Panel', 'QWidget#playlist_panel', ''),
            ('Playlist Selector', 'QComboBox#playlist_selector', ''),
            ('Selector: Arrow Area', 'QComboBox#playlist_selector', '::drop-down'),
            ('Selector: Arrow', 'QComboBox#playlist_selector', '::down-arrow'),
            ('Track List', 'QListWidget#playlist_tracks', ''),
            ('Track Items', 'QListWidget#playlist_tracks', '::item'),
            ('Shuffle Button', 'QPushButton#playlist_shuffle', ''),
            ('Playlist Menu Button (…)', 'QPushButton#playlist_refresh', ''),
            ('Playlist Context Menu', 'QMenu#playlist_context_menu', ''),
            ('Menu Items', 'QMenu#playlist_context_menu', '::item'),
            ('Menu Separators', 'QMenu#playlist_context_menu', '::separator'),
            ('Track Counter', 'QLabel#playlist_status', ''),
        ]),
        ('transport', 'Player Buttons (Individual)', [
            ('Previous Track (⏮)', 'QPushButton[spyderThemeId="transport_previous"]', ''),
            ('Play / Pause (⏯)', 'QPushButton[spyderThemeId="transport_play_pause"]', ''),
            ('Stop (⏹)', 'QPushButton[spyderThemeId="transport_stop"]', ''),
            ('Next Track (⏭)', 'QPushButton[spyderThemeId="transport_next"]', ''),
            ('Shared Button Style', 'QPushButton', ''),
        ]),
        ('actions', 'Action Buttons (Individual)', [
            ('STOP AFTER Button', 'QPushButton[spyderThemeId="stop_after"]', ''),
            ('FADE STOP Button', 'QPushButton[spyderThemeId="fade_stop"]', ''),
            ('REPEAT 1 Button', 'QPushButton[spyderThemeId="repeat_one"]', ''),
        ]),
        ('search', 'Search Bar & Queue Button', [
            ('Search Bar Frame', 'QFrame#search_panel', ''),
            ('Search Input', 'QComboBox', ''),
            ('Search Field: Arrow Area', 'QComboBox', '::drop-down'),
            ('Search Field: Arrow', 'QComboBox', '::down-arrow'),
            ('QUEUE Button', 'QWidget[spyderThemeId="queue_button"]', ''),
        ]),
        ('rating', 'Rating Controls', [
            ('Thumbs Down (−5)', 'QWidget[spyderThemeId="negative_rating"]', ''),
            ('Thumbs Up (+5)', 'QWidget[spyderThemeId="positive_rating"]', ''),
            ('Score Display', 'QWidget[spyderThemeId="rating_score"]', ''),
            ('Ban Button (🚫)', 'QWidget[spyderThemeId="ban_button"]', ''),
        ]),
        ('quick', 'Quick Settings', [
            ('Quick Settings Frame', 'QFrame#quick_panel', ''),
            ('Labels', 'QLabel#quickSettingLabel', ''),
            ('Genre Input', 'QWidget[spyderThemeId="genres"]', ''),
            ('Track Lock Input', 'QWidget[spyderThemeId="track_lock"]', ''),
            ('Artist Lock Input', 'QWidget[spyderThemeId="artist_lock"]', ''),
            ('Top Count Input', 'QWidget[spyderThemeId="top_count"]', ''),
            ('Preset Selection', 'QComboBox', ''),
            ('APPLY Button', 'QPushButton[spyderThemeId="quick_apply"]', ''),
            ('Temperature Track', 'QSlider[spyderThemeId="temperature"]', '::groove:horizontal'),
            ('Temperature Fill', 'QSlider[spyderThemeId="temperature"]', '::sub-page:horizontal'),
            ('Temperature Handle', 'QSlider[spyderThemeId="temperature"]', '::handle:horizontal'),
            ('Temperature Value Display', 'QLabel[spyderThemeId="temperature_value"]', ''),
        ]),
        ('common', 'Global Default Elements', [
            ('All Standard Buttons', 'QPushButton', ''),
            ('All Text Inputs', 'QLineEdit', ''),
            ('All Dropdown Menus', 'QComboBox', ''),
            ('Checkbox: Indicator', 'QCheckBox', '::indicator'),
            ('Checkbox: Label', 'QCheckBox', ''),
            ('Scrollbars', 'QScrollBar', ''),
        ]),
    ]

    def _build_preview(self):
        self._group_lookup = {key: (name, parts) for key, name, parts in self.GROUPS}
        self.preview.setStyleSheet('background: #090d13;')

        top_title = QLabel('PREVIEW AREA (LIVE COCKPIT)')
        top_title.setAlignment(Qt.AlignCenter)
        top_title.setStyleSheet('font-weight: bold; color: #58a6ff; font-size: 11px; padding: 2px;')
        self.pv.addWidget(top_title)

        # 1. Tabs
        self._mini_btn('tabs', '☰ Live Cockpit   Expert Settings   AI Training   Vibe Brain   About', height=26)

        # 2. Obere Reihe
        row1 = QHBoxLayout()
        row1.setSpacing(5)
        self.pv.addLayout(row1)

        # Preview-only PEQ sample; the real album cover remains independent.
        peq_preview = self._mini_btn('peq',
            'PARAMETRIC EQ\\n\\n20 Hz  ─╮╭─╮─  20 kHz\\n'
            'Bell · Shelf · Notch', height=125, layout=row1, stretch=1)
        peq_preview.setObjectName('peq_preview')

        self._mini_btn('brain', 'AI-Log\n(Decisions & Vibe)\n\nScout Status Bar', height=125, layout=row1, stretch=3)
        self._mini_btn('top', 'Top Artists\n\n(Artist List)', height=125, layout=row1, stretch=2)
        self._mini_btn('history', 'Last Songs\n\n(History)', height=125, layout=row1, stretch=2)

        # 3. Mittlere Reihe
        row2 = QHBoxLayout()
        row2.setSpacing(5)
        self.pv.addLayout(row2)

        # Three independent, clickable preview panels. These proportions affect
        # only QSS Studio's mock cockpit, never the active theme geometry.
        self._mini_btn('main', 'MAIN-LOG\n(Track Display & Log Text)\n\nVU-Meter (Level Display)',
                       height=110, layout=row2, stretch=2)

        queue_container = QFrame()
        queue_container.setStyleSheet('background: transparent; border: none;')
        qc_layout = QVBoxLayout(queue_container)
        qc_layout.setContentsMargins(0, 0, 0, 0)
        qc_layout.setSpacing(3)
        q_list_btn = self._mini_btn('queue', 'Queue\n\n(Track List)', height=80, layout=None)
        q_clear_btn = QPushButton('Clear Queue')
        q_clear_btn.setProperty('m2Group', 'queue')
        q_clear_btn.setCursor(Qt.PointingHandCursor)
        q_clear_btn.setFixedHeight(25)
        q_clear_btn.setStyleSheet(
            'QPushButton { font-size: 10px; font-weight: bold; color: #ff7b72; background: #21262d; '
            'border: 1px solid #30363d; border-radius: 4px; }'
            'QPushButton:hover { border-color: #ff7b72; background: #30363d; }'
        )
        q_clear_btn.clicked.connect(lambda _=False: self._choose_group('queue'))
        qc_layout.addWidget(q_list_btn, 1)
        qc_layout.addWidget(q_clear_btn)
        row2.addWidget(queue_container, 2)

        playlist_container = QFrame()
        playlist_container.setStyleSheet('background: transparent; border: none;')
        pl_layout = QVBoxLayout(playlist_container)
        pl_layout.setContentsMargins(0, 0, 0, 0)
        pl_layout.setSpacing(3)
        pl_header = QHBoxLayout()
        pl_header.setSpacing(3)
        pl_select = QPushButton('Playlist ▾')
        pl_select.setObjectName('playlist_selector_preview')
        pl_select.setProperty('qssSelector', 'QComboBox#playlist_selector')
        pl_select.clicked.connect(lambda: self._choose_element('QComboBox#playlist_selector'))
        pl_header.addWidget(pl_select, 1)
        pl_shuffle = QPushButton('Shuffle')
        pl_shuffle.setCheckable(True)
        pl_shuffle.setObjectName('playlist_shuffle')
        pl_shuffle.setProperty('qssSelector', 'QPushButton#playlist_shuffle')
        pl_shuffle.clicked.connect(lambda: self._choose_element('QPushButton#playlist_shuffle'))
        pl_header.addWidget(pl_shuffle)
        pl_menu = QPushButton('…')
        pl_menu.setObjectName('playlist_refresh')
        pl_menu.setProperty('qssSelector', 'QPushButton#playlist_refresh')
        pl_menu.clicked.connect(self._playlist_menu_clicked)
        self._playlist_menu_clicks = 0
        pl_header.addWidget(pl_menu)
        # Match the other preview controls rather than rendering an unstyled text row.
        for control in (pl_select, pl_shuffle, pl_menu):
            control.setCursor(Qt.PointingHandCursor)
            control.setFixedHeight(27)
            control.setStyleSheet(
                'QPushButton { color: #c9d1d9; background: #21262d; '
                'border: 1px solid #30363d; border-radius: 4px; '
                'padding: 3px 7px; font-size: 10px; }'
                'QPushButton:hover { background: #30363d; border-color: #58a6ff; }'
                'QPushButton:checked { background: #1c2736; border-color: #58a6ff; }'
            )
        pl_menu.setFixedWidth(32)
        pl_shuffle.setText('Shuffle')
        pl_layout.addLayout(pl_header)
        self._mini_btn('playlist', 'Track List', height=65,
                       layout=pl_layout, stretch=1)
        self._mini_btn('playlist', 'Track Counter', height=25,
                       layout=pl_layout, stretch=0)
        row2.addWidget(playlist_container, 2)

        # 4. Individual player and action buttons
        row3 = QHBoxLayout()
        row3.setSpacing(5)
        self.pv.addLayout(row3)

        self._mini_single_btn('transport', '⏮ Prev', row3)
        self._mini_single_btn('transport', '⏯ Play/Pause', row3)
        self._mini_single_btn('transport', '⏹ Stop', row3)
        self._mini_single_btn('transport', '⏭ Next', row3)

        row3.addSpacing(12)

        self._mini_single_btn('actions', '🛑 STOP AFTER', row3)
        self._mini_single_btn('actions', '📉 FADE STOP', row3)
        self._mini_single_btn('actions', '🔂 REPEAT 1', row3)

        # 5. Suchleiste & Bewertung
        row4 = QHBoxLayout()
        row4.setSpacing(5)
        self.pv.addLayout(row4)

        self._mini_btn('search', 'Search: [ Enter title / artist... ▾ ]  [ QUEUE ]', height=32, layout=row4, stretch=5)
        self._mini_btn('rating', '👎 −5   10 Pts   👍 +5   🚫 Ban', height=32, layout=row4, stretch=3)

        # 6. Schnelleinstellungen
        self._mini_btn('quick', 'Quick Settings:  Preset ▾   Genres   Lock   Top   |   Temp: ──●── 0.3   [ APPLY ]', height=32)

        # 7. Checkbox-Muster
        cb_row = QHBoxLayout()
        self.pv.addLayout(cb_row)
        self.checkbox_sample = QCheckBox('Checkbox: Example (click to toggle)')
        self.checkbox_sample.setObjectName('m2CheckboxSample')
        self.checkbox_sample.setCursor(Qt.PointingHandCursor)
        self.checkbox_sample.setChecked(True)
        self.checkbox_sample.clicked.connect(self._select_checkbox_from_preview)
        cb_row.addWidget(self.checkbox_sample)
        cb_row.addStretch(1)

    def _mini_single_btn(self, group_key, text, layout):
        b = QPushButton(text)
        b.setProperty('m2Group', group_key)
        b.setCursor(Qt.PointingHandCursor)
        b.setFixedHeight(32)
        b.setStyleSheet(
            'QPushButton { font-weight: bold; font-size: 10px; color: #f0f6fc; '
            'background: #21262d; border: 1px solid #30363d; border-radius: 4px; padding: 3px 6px; }'
            'QPushButton:hover { border-color: #58a6ff; background: #30363d; }'
        )
        b.clicked.connect(lambda _=False, g=group_key: self._choose_group(g))
        layout.addWidget(b, 1)
        return b

    def _mini_btn(self, key, text, height=34, layout=None, stretch=1):
        b = QPushButton(text)
        b.setProperty('m2Group', key)
        b.setCursor(Qt.PointingHandCursor)
        b.setFixedHeight(height)
        b.setStyleSheet(
            'QPushButton { text-align: left; padding: 5px 8px; font-family: "Consolas", monospace; font-size: 10px; '
            'color: #c9d1d9; background: #161b22; border: 1px solid #30363d; border-radius: 5px; }'
            'QPushButton:hover { border: 1px solid #58a6ff; background: #1f242c; }'
        )
        b.clicked.connect(lambda _=False, g=key: self._choose_group(g))
        if layout is not None:
            layout.addWidget(b, stretch)
        else:
            self.pv.addWidget(b)
        return b

    def _select_checkbox_from_preview(self, checked=False):
        self._choose_group('common')
        self._choose_element('QCheckBox', '::indicator')
        for i in range(self.state.count()):
            if self.state.itemData(i) == (':checked' if checked else ''):
                self.state.setCurrentIndex(i)
                break

    def _highlight_preview_selection(self, active_key):
        for btn in self.preview.findChildren(QPushButton):
            group = btn.property('m2Group')
            if not group:
                continue
            if group == active_key:
                btn.setStyleSheet(
                    btn.styleSheet().replace('#30363d', '#58a6ff').replace('#161b22', '#1c2736') + '; border: 2px solid #58a6ff;'
                )
            else:
                btn.setStyleSheet(
                    re.sub(r'; border: 2px solid #58a6ff;', '', btn.styleSheet()).replace('#58a6ff', '#30363d').replace('#1c2736', '#161b22')
                )

    def _choose_group(self, key):
        if key not in self._group_lookup: return
        self._selected_group = key
        title, options = self._group_lookup[key]
        self.group_title.setText(title)
        
        grouped = {}
        for label, base, suffix in options:
            if base not in grouped:
                grouped[base] = {'title': label, 'parts': []}
            if suffix not in [x[1] for x in grouped[base]['parts']]:
                grouped[base]['parts'].append((label, suffix))
        self._grouped_choices = grouped
        
        self.element_choice.blockSignals(True)
        self.element_choice.clear()
        for base, info in grouped.items():
            label = info['title']
            self.element_choice.addItem(label, base)
        self.element_choice.blockSignals(False)
        self.element_choice.setCurrentIndex(0)
        self._choice_changed(0)
        self._highlight_preview_selection(key)
        self._update_title_controls()

    TITLE_DEFAULTS = {
        'brain': 'AI Judge & Vibe Selection',
        'main': '',
        'top': 'Top X',
        'queue': 'Queue',
        'history': 'Last Songs',
    }

    def _update_title_controls(self):
        key = self._selected_group
        self.title_box.setVisible(key in self.TITLE_DEFAULTS)
        if key not in self.TITLE_DEFAULTS:
            return
        cfg = self._panel_titles.get(key, {})
        self.title_text.blockSignals(True)
        self.title_visible.blockSignals(True)
        self.title_text.setText(cfg.get('text', self.TITLE_DEFAULTS[key]))
        self.title_visible.setChecked(cfg.get('visible', True))
        self.title_text.blockSignals(False)
        self.title_visible.blockSignals(False)

    def _title_edited(self, value):
        key = self._selected_group
        if key in self.TITLE_DEFAULTS:
            self._panel_titles.setdefault(key, {})['text'] = value

    def _title_visibility_changed(self, visible):
        key = self._selected_group
        if key in self.TITLE_DEFAULTS:
            self._panel_titles.setdefault(key, {})['visible'] = bool(visible)

    def result_panel_titles(self):
        return {k: dict(v) for k, v in self._panel_titles.items()}

    def _choose_element(self, selector, suffix=None):
        if selector != 'QPushButton#playlist_refresh':
            self._playlist_menu_clicks = 0
        # A preview click may target an element from another category. Switch
        # the category first, otherwise the old combobox silently ignores it.
        if not any(self.element_choice.itemData(i) == selector
                   for i in range(self.element_choice.count())):
            for group_key, _title, options in self.GROUPS:
                if any(base == selector for _label, base, _part in options):
                    self._choose_group(group_key)
                    break
        for i in range(self.element_choice.count()):
            if self.element_choice.itemData(i) == selector:
                self.element_choice.setCurrentIndex(i)
                if suffix is not None:
                    self._select_part(selector, suffix)
                return

    def _choice_changed(self, index):
        if index < 0: return
        base = self.element_choice.itemData(index)
        if not base: return
        entries = self._grouped_choices.get(base, {}).get('parts', [])
        suffix = entries[0][1] if entries else ''
        self._select_part(base, suffix)
        self._rebuild_component_buttons(base, entries)
        self._update_relevant_fields(base, suffix)

    def _rebuild_component_buttons(self, base, entries):
        for btn in self.component_part_buttons:
            self.component_parts_row.removeWidget(btn)
            btn.deleteLater()
        self.component_part_buttons = []
        if len(entries) == 1:
            available = self.PARTS.get(base)
            if available and len(available) > 1:
                entries = available
        for label, suffix in entries:
            short = label.split(' – ')[-1] if ' – ' in label else label
            btn = QPushButton(short)
            btn.setObjectName('qssStudioComponentButton')
            btn.setStyleSheet('''
                QPushButton#qssStudioComponentButton {
                    background: #292929; color: #c9d1d9;
                    border: 1px solid #3a3a3a; border-radius: 4px;
                    padding: 5px 8px;
                }
                QPushButton#qssStudioComponentButton:hover {
                    background: #353535; border-color: #58a6ff;
                }
                QPushButton#qssStudioComponentButton:checked {
                    background: #237cb2; color: white; border-color: #58a6ff;
                }
            ''')
            btn.setCheckable(True)
            btn.clicked.connect(lambda _, b=base, sf=suffix: self._select_part(b, sf))
            self.component_parts_row.addWidget(btn)
            self.component_part_buttons.append(btn)
        self.component_parts.setVisible(len(entries) > 1 or base.startswith('QSlider'))
        self._sync_component_buttons()

    def _sync_component_buttons(self):
        suffix = self.part.currentData() or ''
        base = self.selector.currentText().strip()
        entries = self._grouped_choices.get(base, {}).get('parts', [])
        if len(entries) == 1:
            available = self.PARTS.get(base)
            if available and len(available) > 1:
                entries = available
        for btn, (_, part_suffix) in zip(self.component_part_buttons, entries):
            btn.blockSignals(True)
            btn.setChecked(part_suffix == suffix)
            btn.blockSignals(False)

    def _update_relevant_fields(self, base, suffix):
        indicator = base in ('QCheckBox', 'QRadioButton') and suffix == '::indicator'
        caption = base in ('QCheckBox', 'QRadioButton') and suffix == ''
        non_text = ('QFrame', 'QSlider', 'QScrollBar', 'QProgressBar')
        text_ok = (not base.startswith(non_text)) and not suffix.startswith(
            ('::groove', '::handle', '::sub-page', '::chunk', '::indicator'))
        # Item states already have their own Normal/Hover/Selected rules.
        # Selection palette properties belong to text selection in editors,
        # not to QListWidget::item or its parent list widget.
        selection_ok = (suffix == '' and
                        (base.startswith(('QLineEdit', 'QPlainTextEdit',
                                          'QTextEdit'))))
        
        visible = {
            'background': not caption,
            'color': text_ok,
            'border-color': not caption,
            'selection-background-color': selection_ok and not indicator,
            'selection-color': selection_ok and not indicator,
            'border-width': not caption,
            'border-radius': not caption,
        }
        for key, show in visible.items():
            widget = self.fields[key]
            label = self.form.labelForField(widget)
            if label: label.setVisible(show)
            widget.setVisible(show)
        is_peq_plot = (base == 'QWidget#peq_plot' and suffix == '')
        for button in self.peq_color_fields.values():
            button.setVisible(is_peq_plot)
            label = self.form.labelForField(button)
            if label: label.setVisible(is_peq_plot)
        for widget in (self.family, self.size, self.bold):
            widget.setVisible(text_ok)
            label = self.form.labelForField(widget)
            if label: label.setVisible(text_ok)
            
        has_states = (base.startswith('QPushButton')
                      or 'spyderThemeId' in base
                      or base in ('QSlider', 'QTabBar', 'QComboBox', 'QLineEdit', 'QCheckBox', 'QRadioButton')
                      or base.startswith('QComboBox#')
                      or suffix == '::item')
        self.state.setVisible(has_states)
        self.state_label.setVisible(has_states)
        if indicator:
            self.state_label.setText('Indicator State')
        elif caption:
            self.state_label.setText('Label State')
        else:
            self.state_label.setText('State (supported states only)')

    def _playlist_menu_clicked(self):
        # First click selects the menu button; second click previews the popup.
        self._playlist_menu_clicks += 1
        if self._playlist_menu_clicks == 1:
            self._choose_element('QPushButton#playlist_refresh')
        else:
            self._playlist_menu_clicks = 0
            self._show_playlist_menu_preview()

    def _show_playlist_menu_preview(self):
        self._choose_element('QMenu#playlist_context_menu')
        menu = QMenu(self)
        menu.setObjectName('playlist_context_menu')
        menu.setMinimumWidth(220)
        menu.addAction('New Playlist…')
        menu.addAction('Rename Playlist…')
        menu.addAction('Delete Playlist…')
        menu.addSeparator()
        menu.addAction('Shuffle Playlist Order')
        disabled = menu.addAction('Unavailable Action')
        disabled.setEnabled(False)
        menu.exec(self.preview.mapToGlobal(self.preview.rect().center()))

    def _wire_preview_children(self):
        for widget in [self.preview] + self.preview.findChildren(QWidget):
            widget.installEventFilter(self)

    def _selector_for_widget(self, widget):
        current = widget
        while current is not None and current is not self.preview:
            if isinstance(current, QTabBar):
                return 'QTabBar'
            selector = current.property('qssSelector')
            if selector:
                return selector
            current = current.parentWidget()
        return None

    def eventFilter(self, obj, event):
        if event.type() == QEvent.MouseButtonPress and isinstance(obj, QWidget):
            selector = self._selector_for_widget(obj)
            if selector:
                self._choose_element(selector)
        return super().eventFilter(obj, event)

    def _select_part(self, base, suffix):
        self._select(base)
        for i in range(self.part.count()):
            if self.part.itemData(i) == suffix:
                self.part.setCurrentIndex(i)
                break

    def _rules(self):
        return [(m.group(1).strip(), m.group(2)) for m in RULE.finditer(self.source.toPlainText())
                if not m.group(1).strip().startswith('/*')]

    def _source_changed(self):
        self._busy = True
        old = self.selector.currentText()
        old_part = self.part.currentData()
        old_state = self.state.currentData()
        choices = []
        for _, _, options in self.GROUPS:
            for _, base, _ in options:
                if base not in choices: choices.append(base)
        for selector, _ in self._rules():
            for part in selector.split(','):
                p = part.strip()
                if p and p not in choices: choices.append(p)
        self.selector.clear()
        self.selector.addItems(choices)
        self.selector.setCurrentText(old if old else 'QPushButton')
        self._busy = False
        self._apply_preview()
        self._base_changed()
        for i in range(self.part.count()):
            if self.part.itemData(i) == old_part:
                self.part.setCurrentIndex(i)
                break
        for i in range(self.state.count()):
            if self.state.itemData(i) == old_state:
                self.state.setCurrentIndex(i)
                break
        self._refresh_properties()

    def _state_changed(self, *_):
        if self._busy: return
        self._sync_checkbox_preview_state()
        self._refresh_properties()

    def _sync_checkbox_preview_state(self):
        if not hasattr(self, 'checkbox_sample'): return
        base = self.selector.currentText().strip()
        if base != 'QCheckBox':
            self.checkbox_sample.setEnabled(True)
            return
        state = self.state.currentData() or ''
        sample = self.checkbox_sample
        sample.setEnabled(state != ':disabled')
        if state == ':checked':
            sample.setChecked(True)
        elif state in ('', ':hover', ':disabled'):
            sample.setChecked(False)
        sample.style().unpolish(sample)
        sample.style().polish(sample)
        sample.update()

    def _apply_preview(self):
        if not hasattr(self, 'checkbox_sample'): return
        relevant = []
        for selector, body in self._rules():
            names = [x.strip() for x in selector.split(',')]
            matching = [name for name in names if name == 'QCheckBox'
                        or name.startswith('QCheckBox:')
                        or name.startswith('QCheckBox::')]
            if matching:
                relevant.append(', '.join(matching) + ' {' + body + '}')
        self.checkbox_sample.setStyleSheet('\n'.join(relevant))
        self._sync_checkbox_preview_state()

    def _select(self, selector):
        self._busy = True
        self.selector.setCurrentText(selector)
        self.state.setCurrentIndex(0)
        self._busy = False
        self._base_changed()

    PARTS = {
        'QPushButton': [('Entire button', '')],
        'QLineEdit': [('Input Field', '')],
        'QComboBox': [('Input Field', ''), ('Arrow Area', '::drop-down'), ('Arrow', '::down-arrow')],
        'QCheckBox': [('Label', ''), ('Indicator', '::indicator')],
        'QTabBar': [('Tab Area / Text', '::tab')],
        'QListWidget': [('List', ''), ('Item', '::item')],
        'QPlainTextEdit': [('Text field', '')],
        'QListWidget#top_list': [('List (Top 100)', ''), ('Item (::item)', '::item')],
        'QListWidget#history_list': [('List (Last Songs)', ''), ('Item (::item)', '::item')],
        'QListWidget#queue_list': [('List (Queue)', ''), ('Item (::item)', '::item')],
        'QComboBox#playlist_selector': [('Playlist Selector', ''), ('Arrow Area', '::drop-down'), ('Arrow', '::down-arrow')],
        'QListWidget#playlist_tracks': [('Track List', ''), ('Track Items', '::item')],
        'QWidget#playlist_panel': [('Playlist Panel', '')],
        'QPushButton#playlist_shuffle': [('Shuffle Button', '')],
        'QPushButton#playlist_refresh': [('Playlist Menu Button (…)', '')],
        'QMenu#playlist_context_menu': [('Menu Background', ''), ('Menu Items', '::item'), ('Menu Separators', '::separator')],
        'QLabel#playlist_status': [('Track Counter', '')],
        'QPlainTextEdit#ai_log': [('Text Area (AI Log)', '')],
        'QPlainTextEdit#main_log_text': [('Text Area (Main Log)', '')],
        'QFrame#brain_panel': [('Container Frame (Brain)', '')],
        'QFrame#main_log_panel': [('Container Frame (Main Log)', '')],
        'QFrame#top_artists_panel': [('Container Frame (Top 100)', '')],
        'QFrame#history_panel': [('Container Frame (Last Songs)', '')],
        'QFrame#queue_panel': [('Container Frame (Queue)', '')],
        'QFrame#search_panel': [('Search Bar Frame', '')],
        'QFrame#quick_panel': [('Quick Settings Frame', '')],
        'QFrame#scout_strip': [('Scout Bar Frame', '')],
        'QLabel#brain_header': [('Header (Brain)', '')],
        'QLabel#track_title': [('Track Title Display', '')],
        'QLabel#top_artists_header': [('Header (Top 100)', '')],
        'QLabel#history_header': [('Header (Last Songs)', '')],
        'QLabel#queue_header': [('Header (Queue)', '')],
        'QSlider[spyderThemeId="temperature"]': [
            ('Temperature Track', '::groove:horizontal'),
            ('Temperature Fill', '::sub-page:horizontal'),
            ('Temperature Handle', '::handle:horizontal')
        ],
    }

    STATES = {
        'normal': [('Normal', '')],
        'interactive': [
            ('Normal', ''),
            ('Hover (Mouse Over)', ':hover'),
            ('Pressed (Click)', ':pressed'),
            ('Disabled', ':disabled')
        ],
        'toggle_button': [
            ('Normal (Off)', ''),
            ('Hover (Mouse Over)', ':hover'),
            ('Pressed (Click)', ':pressed'),
            ('Active / On', '[spyderActive="true"]'),
            ('Disabled', ':disabled')
        ],
        'menu_item': [('Normal', ''), ('Selected / Hover', ':selected'), ('Disabled', ':disabled')],
        'check': [
            ('Normal (Off)', ''),
            ('Hover (Mouse Over)', ':hover'),
            ('Active / Checked', ':checked'),
            ('Disabled', ':disabled')
        ],
        'tab': [
            ('Normal', ''),
            ('Hover (Mouse Over)', ':hover'),
            ('Selected / Active', ':selected'),
            ('Disabled', ':disabled')
        ],
        'item': [
            ('Normal', ''),
            ('Hover (Mouse Over)', ':hover'),
            ('Selected', ':selected')
        ],
    }

    def _base_changed(self, *_):
        if self._busy: return
        self._busy = True
        base = self.selector.currentText().strip()
        if base in self.PARTS:
            parts = self.PARTS[base]
        elif base.startswith('QSlider[') and 'spyderThemeId=' in base:
            parts = self.PARTS.get('QSlider[spyderThemeId="temperature"]', [('Selected QSS Rule', '')])
        else:
            parts = [('Selected QSS Rule', '')]
        self.part.clear()
        for label, suffix in parts:
            self.part.addItem(label, suffix)
        self._busy = False
        self._selection_changed()

    def _selection_changed(self, *_):
        if self._busy: return
        self._busy = True
        base = self.selector.currentText().strip()
        suffix = self.part.currentData() or ''
        
        if base in ('QPushButton[spyderThemeId="stop_after"]', 'QPushButton[spyderThemeId="repeat_one"]'):
            kind = 'toggle_button'
        elif base == 'QTabBar' or base == 'QTabBar::tab':
            kind = 'tab'
        elif base in ('QCheckBox', 'QRadioButton'):
            kind = 'check'
        elif base == 'QPushButton#playlist_shuffle':
            kind = 'check'
        elif base == 'QMenu#playlist_context_menu' and suffix == '::item':
            kind = 'menu_item'
        elif suffix == '::item':
            kind = 'item'
        elif suffix in ('::groove:horizontal', '::sub-page:horizontal', '::chunk', '::pane', '::tab-bar'):
            kind = 'normal'
        elif (base.startswith('QPushButton') or 'spyderThemeId' in base
              or base in ('QComboBox', 'QLineEdit', 'QScrollBar')
              or base.startswith('QComboBox#')
              or suffix in ('::handle:horizontal','::drop-down','::down-arrow')):
            kind = 'interactive'
        else:
            kind = 'normal'
            
        self._sync_component_buttons()
        self.state.clear()
        for label, token in self.STATES[kind]:
            self.state.addItem(label, token)
        self._busy = False
        self._update_relevant_fields(base, suffix)
        self._refresh_properties()

    def _effective_selector(self):
        base = self.selector.currentText().strip()
        if not base: return ''
        part = self.part.currentData() or ''
        state = self.state.currentData() or ''
        return base + part + state

    def _get_properties(self, selector):
        props = {}
        for m in RULE.finditer(self.source.toPlainText()):
            if selector in [re.sub(r'/\*.*?\*/', '', x, flags=re.S).strip() for x in m.group(1).split(',')]:
                for key, val in re.findall(r'([\w-]+)\s*:\s*([^;{}]+)', m.group(2)):
                    props[key.strip()] = val.strip()
        if 'background' not in props and 'background-color' in props:
            props['background'] = props['background-color']
        return props

    def _refresh_properties(self, *_):
        if self._busy: return
        s = self._effective_selector()
        props = self._get_properties(s)
        self._busy = True
        for key in ('background','color','border-color','selection-background-color','selection-color'):
            value = props.get('background-color', props.get('background', '(geerbt)')) if key == 'background' else props.get(key, '(geerbt)')
            self.fields[key].setText(value)
            c = QColor(value)
            if not c.isValid() and value.startswith('rgba('):
                channels = re.findall(r'\d+', value)
                if len(channels) == 4:
                    c = QColor(*map(int, channels))
            self.fields[key].setStyleSheet(f'background-color: {value};' if c.isValid() else '')
        for key, button in self.peq_color_fields.items():
            value = props.get(key, '(inherited)')
            button.setText(value)
            color = QColor(value)
            button.setStyleSheet(f'background-color: {value};' if color.isValid() else '')
        family = props.get('font-family','Arial').split(',')[0].strip(' "\'')
        self.family.setCurrentFont(QFont(family))
        size = re.search(r'\d+', props.get('font-size',''))
        self.size.setValue(int(size.group()) if size else 0)
        self.bold.setChecked(props.get('font-weight','') == 'bold')
        for key in ('border-width','border-radius'):
            n = re.search(r'\d+', props.get(key,''))
            self.fields[key].setValue(int(n.group()) if n else 0)
            
        self.slider_height.blockSignals(True)
        self.handle_width.blockSignals(True)
        height = re.search(r'\d+', props.get('height', ''))
        width = re.search(r'\d+', props.get('width', ''))
        self.slider_height.setValue(int(height.group()) if height else 7)
        self.handle_width.setValue(int(width.group()) if width else 13)
        self.slider_height.blockSignals(False)
        self.handle_width.blockSignals(False)
        
        is_track = 'groove' in s or 'sub-page' in s
        is_handle = 'handle' in s and 'temperature' in s
        self.slider_height_label.setVisible(is_track)
        self.slider_height.setVisible(is_track)
        self.handle_width_label.setVisible(is_handle)
        self.handle_width.setVisible(is_handle)
        
        self._sync_checkbox_preview_state()
        note = ('Normal, Hover and Selected are separate item rules.'
                if '::item' in s else
                'The playlist panel is the outer container; its child widgets can cover its background.'
                if s.startswith('QWidget#playlist_panel') else
                'Selection colors affect highlighted text inside editable inputs.'
                if self.fields['selection-background-color'].isVisible() else
                'Sub-elements are styled independently; unset values are inherited.')
        self.info.setText(f'Rule: {s}\n{note}')
        self._busy = False

    def _choose_color(self, key):
        props = self._get_properties(self._effective_selector())
        value = props.get('background-color', props.get('background', '#ffffff')) if key == 'background' else props.get(key, '#ffffff')
        initial = QColor(value)
        if not initial.isValid() and value.startswith('rgba('):
            channels = re.findall(r'\d+', value)
            if len(channels) == 4:
                initial = QColor(*map(int, channels))
        color = QColorDialog.getColor(initial if initial.isValid() else QColor('#ffffff'), self,
                                      'Choose Color', QColorDialog.ShowAlphaChannel)
        if color.isValid():
            val = (f'rgba({color.red()}, {color.green()}, {color.blue()}, {color.alpha()})'
                   if color.alpha() != 255 else color.name())
            self._write(key, val)

    @classmethod
    def _ensure_specific_slider_geometry(cls, text, base):
        defaults = (
            ('::groove:horizontal', {'height': '7px', 'background': '#444444', 'border-radius': '3px'}),
            ('::sub-page:horizontal', {'border-radius': '3px'}),
            ('::handle:horizontal', {'width': '13px', 'height': '13px', 'margin': '-3px 0', 'background': '#888888', 'border-radius': '6px'}),
        )
        for suffix, required in defaults:
            selector = base + suffix
            found = any(selector in [part.strip() for part in m.group(1).split(',')]
                        for m in RULE.finditer(text))
            if not found:
                text = text.rstrip() + f'\n\n{selector} {{\n}}\n'
            text = cls._complete_rule_properties(text, selector, required)
        return text

    @staticmethod
    def _complete_rule_properties(text, selector, defaults):
        matches = [m for m in RULE.finditer(text)
                   if selector in [re.sub(r'/\*.*?\*/', '', x, flags=re.S).strip()
                                   for x in m.group(1).split(',')]]
        if not matches:
            return text
        m = matches[-1]
        body = m.group(2)
        existing = {d.group(1).strip(): d.group(2).strip()
                    for d in re.finditer(r'([\w-]+)\s*:\s*([^;{}]+)', body)}
        additions = []
        for key, value in defaults.items():
            if key not in existing:
                additions.append(f'    {key}: {value};')
        if additions:
            body = body.rstrip() + '\n' + '\n'.join(additions) + '\n'
            return text[:m.start(2)] + body + text[m.end(2):]
        return text

    @classmethod
    def _complete_border_rule(cls, text, selector):
        matches = [m for m in RULE.finditer(text)
                   if selector in [re.sub(r'/\*.*?\*/', '', x, flags=re.S).strip()
                                   for x in m.group(1).split(',')]]
        if not matches:
            return text
        body = matches[-1].group(2)
        props = {d.group(1).strip(): d.group(2).strip()
                 for d in re.finditer(r'([\w-]+)\s*:\s*([^;{}]+)', body)}
        if 'border' in props or 'border-style' in props:
            return text
        if any(k in props for k in ('border-top-style', 'border-right-style',
                                     'border-bottom-style', 'border-left-style')):
            return text
        defaults = {'border-style': 'solid'}
        if 'border-width' not in props:
            defaults['border-width'] = '1px'
        return cls._complete_rule_properties(text, selector, defaults)

    @classmethod
    def _complete_indicator_geometry(cls, text, selector):
        return cls._complete_rule_properties(text, selector,
                                             {'width': '16px', 'height': '16px'})

    def _write(self, key, value):
        if self._busy: return
        selector = self._effective_selector()
        if not selector: return
        text = self.source.toPlainText()
        
        if selector.startswith('QSlider[spyderThemeId="temperature"]') and '::' in selector:
            base_selector = selector.split('::', 1)[0]
            text = self._ensure_specific_slider_geometry(text, base_selector)
            
        prop = 'background-color' if key == 'background' else key
        matches = [m for m in RULE.finditer(text)
                   if selector in [re.sub(r'/\*.*?\*/', '', x, flags=re.S).strip() for x in m.group(1).split(',')]]
        if matches:
            m = matches[-1]
            body = m.group(2)
            declarations = list(re.finditer(r'([\w-]+)\s*:\s*([^;{}]+)\s*;?', body))
            same = [d for d in declarations if d.group(1) == prop or (prop == 'background-color' and d.group(1) == 'background')]
            if prop == 'background-color':
                body = re.sub(r'(?<![\w-])(?:background|background-color)\s*:\s*[^;{}]+;?', '', body)
                body = body.rstrip() + f'\n    background-color: {value};\n'
            elif same:
                d = same[-1]
                body = body[:d.start()] + f'{prop}: {value};' + body[d.end():]
            else:
                body = body.rstrip() + f'\n    {prop}: {value};\n'
            text = text[:m.start(2)] + body + text[m.end(2):]
        else:
            text = text.rstrip() + f'\n\n{selector} {{ {prop}: {value}; }}\n'
            
        if prop in ('border-color', 'border-width'):
            text = self._complete_border_rule(text, selector)
        if selector.startswith('QCheckBox::indicator'):
            text = self._complete_indicator_geometry(text, selector)
            
        self._busy = True
        self.source.setPlainText(text)
        self._busy = False
        self._source_changed()

    def result_qss(self):
        return self.source.toPlainText()
