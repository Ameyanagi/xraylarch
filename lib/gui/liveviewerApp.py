#!/usr/bin/env python
"""
GUI for displaying live plots of column data from StepScan Data objects

Principle features:
   frame for plot a file, with math on right/left columns
   fitting frame for simple peak fits
   simple XAS processing (normalization)
"""
import os
import time
import shutil
import numpy as np
from random import randrange

import wx
import wx.lib.agw.flatnotebook as flat_nb
import wx.lib.scrolledpanel as scrolled
import wx.lib.mixins.inspection
from wx._core import PyDeadObjectError

import epics
from epics.wx import DelayedEpicsCallback, EpicsFunction

from larch import Interpreter, use_plugin_path, isParameter
from larch.fitting import fit_report

use_plugin_path('math')
from fitpeak import fit_peak

from wxmplot import PlotFrame, PlotPanel
from xdifile import XDIFile
from ..datafile import StepScanData
from ..scandb import ScanDB
from ..file_utils import fix_filename

from .gui_utils import (SimpleText, FloatCtrl, Closure, pack, add_button,
                        add_menu, add_choice, add_menu, check,
                        CEN, RCEN, LCEN, FRAMESTYLE, Font, hms)

CEN |=  wx.ALL
FILE_WILDCARDS = "Scan Data Files(*.0*,*.dat,*.xdi)|*.0*;*.dat;*.xdi|All files (*.*)|*.*"
FNB_STYLE = flat_nb.FNB_NO_X_BUTTON|flat_nb.FNB_SMART_TABS|flat_nb.FNB_NO_NAV_BUTTONS


PRE_OPS = ('', 'log', '-log', 'deriv', '-deriv', 'deriv(log', 'deriv(-log')
ARR_OPS = ('+', '-', '*', '/')

def randname(n=6):
    "return random string of n (default 6) lowercase letters"
    return ''.join([chr(randrange(26)+97) for i in range(n)])


CURSCAN, SCANGROUP = '< Current Scan >', '_scan_'

class ScanViewerFrame(wx.Frame):
    _about = """Scan Viewer,  Matt Newville <newville @ cars.uchicago.edu>  """
    TIME_MSG = 'Point %i/%i, Time Remaining ~ %s '

    def __init__(self, dbname=None, server='sqlite', host=None,
                 port=None, user=None, password=None, create=True, **kws):

        wx.Frame.__init__(self, None, -1, style=FRAMESTYLE)
        self.data = None
        self.filemap = {}
        title = "Epics Step Scan Viewer"
        self.scandb = None
        if dbname is not None:
            self.scandb = ScanDB(dbname=dbname, server=server, host=host,
                                 user=user, password=password, port=port,
                                 create=create)
            title = '%s, with Live Scan Viewing' % title
        self.larch = None
        self.lgroup = None
        self.plotters = []

        self.SetTitle(title)
        self.SetSize((720, 650))
        self.SetFont(Font(9))

        self.createMainPanel()
        self.createMenus()
        self.statusbar = self.CreateStatusBar(2, 0)
        self.statusbar.SetStatusWidths([-3, -1])
        statusbar_fields = ["Initializing....", " "]
        for i in range(len(statusbar_fields)):
            self.statusbar.SetStatusText(statusbar_fields[i], i)

        if dbname is not None:
            self.get_info  = self.scandb.get_info
            self.live_scanfile = None
            self.live_cpt = -1
            self.total_npts = 1
            self.scantimer = wx.Timer(self)
            self.Bind(wx.EVT_TIMER, self.onScanTimer, self.scantimer)
            self.scantimer.Start(50)

    def onScanTimer(self, evt=None, **kws):
        if self.lgroup is None:
            return

        curfile = fix_filename(self.get_info('filename'))
        sdata = self.scandb.get_scandata()
        npts = len(sdata[-1].data)
        if (npts > 2 and npts == self.live_cpt and
            curfile == self.live_scanfile): # no new data
            return

        # filename changed -- scan starting, so update
        # list of positioners, detectors, etc
        if curfile != self.live_scanfile:
            print 'See new file name ', curfile
            self.live_scanfile = curfile
            self.title.SetLabel(curfile)
            self.lgroup.filename = curfile
            array_labels = [fix_filename(s.name) for s in sdata]
            self.lgroup.array_units = [fix_filename(s.units) for s in sdata]
            self.total_npts = self.get_info('scan_total_points',
                                            as_int=True)
            self.live_cpt = -1
            xcols, ycols, y2cols = [], [], []
            for s in sdata:
                nam = fix_filename(s.name)
                ycols.append(nam)
                if s.notes.startswith('pos'):
                    xcols.append(nam)
            print 'X LABELS ', xcols
            print 'Y LABELS ', ycols

            y2cols = ycols[:] + ['1.0', '0.0', '']
            xarr_old = self.xarr.GetStringSelection()
            self.xarr.SetItems(xcols)
            if xarr_old in xcols:
                self.xarr.SetStringSelection(xarr_old)
            else:
                self.xarr.SetSelection(0)
            for i in range(2):
                for j in range(3):
                    yold = self.yarr[i][j].GetStringSelection()
                    cols = y2cols
                    idef  = 0
                    if i == 0 and j == 0:
                        cols = ycols
                        idef = 1
                    self.yarr[i][j].SetItems(cols)
                    if yold in cols:
                        self.yarr[i][j].SetStringSelection(yold)
                    else:
                        self.yarr[i][j].SetSelection(idef)


        if npts == self.live_cpt:
            return
        time_est = hms(self.get_info('scan_time_estimate', as_int=True))
        msg = self.TIME_MSG % (npts, self.total_npts, time_est)
        self.SetStatusText(msg)
        self.live_cpt = npts
        for row in sdata:
            setattr(self.lgroup, fix_filename(row.name), np.array(row.data))

        if npts > 1:
            self.onPlot()

    def createMainPanel(self):
        wx.CallAfter(self.init_larch)
        mainpanel = wx.Panel(self)
        mainsizer = wx.BoxSizer(wx.VERTICAL)
        panel = wx.Panel(mainpanel)
        sizer = wx.GridBagSizer(8, 7)

        self.title = SimpleText(panel, 'initializing...',
                                font=Font(13), colour='#880000')
        ir = 0
        sizer.Add(self.title, (ir, 1), (1, 6), LCEN, 2)
        # x-axis

        self.xarr = add_choice(panel, choices=[],
                               action=self.onYchoice,  size=(120, -1))

        ir += 1
        sizer.Add(SimpleText(panel, '  X = '), (ir, 0), (1, 1), CEN, 0)
        sizer.Add(self.xarr,                  (ir, 3), (1, 1), RCEN, 0)

        self.yops = [[],[]]
        self.yarr = [[],[]]

        for opts, sel, siz in ((PRE_OPS, 0, 75), (ARR_OPS, 3, 50),
                             (ARR_OPS, 3, 50)):
            w1 = add_choice(panel, choices=opts, action=self.onYchoice,
                            size=(siz, -1))
            w1.SetSelection(sel)
            self.yops[0].append(w1)

            w2 = add_choice(panel, choices=opts, action=self.onYchoice,
                            size=(siz, -1))
            w2.SetSelection(sel)
            self.yops[1].append(w2)

        opts= {'choices':[], 'size':(120, -1), 'action':self.onYchoice}
        for i in range(3):
            self.yarr[0].append(add_choice(panel, **opts))
            self.yarr[1].append(add_choice(panel, **opts))

        for i in range(2):
            ir += 1
            label = '  Y%i = ' % (i+1)
            sizer.Add(SimpleText(panel, label),  (ir, 0), (1, 1), CEN, 0)
            sizer.Add(self.yops[i][0],           (ir, 1), (1, 1), CEN, 0)
            sizer.Add(SimpleText(panel, '[('),   (ir, 2), (1, 1), CEN, 0)
            sizer.Add(self.yarr[i][0],           (ir, 3), (1, 1), CEN, 0)
            sizer.Add(self.yops[i][1],           (ir, 4), (1, 1), CEN, 0)
            sizer.Add(self.yarr[i][1],           (ir, 5), (1, 1), CEN, 0)
            sizer.Add(SimpleText(panel, ')'),    (ir, 6), (1, 1), LCEN, 0)
            sizer.Add(self.yops[i][2],           (ir, 7), (1, 1), CEN, 0)
            sizer.Add(self.yarr[i][2],           (ir, 8), (1, 1), CEN, 0)
            sizer.Add(SimpleText(panel, ']'),    (ir, 9), (1, 1), LCEN, 0)
        ir += 1
        sizer.Add(wx.StaticLine(panel, size=(600, 3),
                                style=wx.LI_HORIZONTAL|wx.GROW),
                  (ir, 0), (1, 12), CEN|wx.GROW|wx.ALL, 0)

        pack(panel, sizer)

        self.plotpanel = PlotPanel(mainpanel, size=(400, 500),
                                   axissize=(0.18, 0.18, 0.70, 0.70),
                                   fontsize=8)

        self.plotpanel.messenger = self.write_message
        self.plotpanel.canvas.figure.set_facecolor((0.98,0.98,0.97))


        btnsizer = wx.StdDialogButtonSizer()
        btnpanel = wx.Panel(mainpanel)
        btnsizer.Add(add_button(btnpanel, 'Pause', action=self.onPause))
        btnsizer.Add(add_button(btnpanel, 'Resume', action=self.onResume))
        btnsizer.Add(add_button(btnpanel, 'Abort', action=self.onAbort))
        pack(btnpanel, btnsizer)

        mainsizer.Add(panel,   0, LCEN|wx.EXPAND, 2)
        mainsizer.Add(self.plotpanel, 1, wx.GROW|wx.ALL, 1)
        mainsizer.Add(btnpanel, 0, wx.GROW|wx.ALL, 1)

        pack(mainpanel, mainsizer)
        return mainpanel

    def onPause(self, evt=None):
        self.scandb.set_info('request_command_pause', 1)

    def onResume(self, evt=None):
        self.scandb.set_info('request_command_pause', 0)

    def onAbort(self, evt=None):
        self.scandb.set_info('request_command_abort', 1)

    def CreateFitPanel(self, parent):
        p = panel = wx.Panel(parent)
        self.fit_model   = add_choice(panel, size=(100, -1),
                                      choices=('Gaussian', 'Lorentzian',
                                               'Voigt', 'Linear', 'Quadratic',
                                               'Step', 'Rectangle',
                                               'Exponential'))
        self.fit_bkg = add_choice(panel, size=(100, -1),
                                  choices=('None', 'constant', 'linear', 'quadtratic'))
        self.fit_step = add_choice(panel, size=(100, -1),
                                  choices=('linear', 'error function', 'arctan'))

        self.fit_report = wx.StaticText(panel, -1, "", (180, 200))
        sizer = wx.GridBagSizer(10, 4)
        sizer.Add(SimpleText(p, 'Fit Model: '),           (0, 0), (1, 1), LCEN)
        sizer.Add(self.fit_model,                         (0, 1), (1, 1), LCEN)

        sizer.Add(SimpleText(p, 'Background: '),          (1, 0), (1, 1), LCEN)
        sizer.Add(self.fit_bkg,                           (1, 1), (1, 1), LCEN)

        sizer.Add(SimpleText(p, 'Step Function Form: '),  (2, 0), (1, 1), LCEN)
        sizer.Add(self.fit_step,                          (2, 1), (1, 1), LCEN)
        sizer.Add(add_button(panel, 'Show Fit', size=(100, -1),
                             action=self.onFitPeak),       (3, 0), (1, 1), LCEN)
        sizer.Add(self.fit_report,                         (0, 2), (4, 2), LCEN, 3)
        pack(panel, sizer)
        return panel

    def CreateXASPanel(self, parent):
        p = panel = wx.Panel(parent)
        self.xas_autoe0   = check(panel, default=True, label='auto?')
        self.xas_autostep = check(panel, default=True, label='auto?')
        self.xas_op       = add_choice(panel, size=(95, -1),
                                       choices=('Raw Data', 'Pre-edged',
                                                'Normalized', 'Flattened'))
        self.xas_e0   = FloatCtrl(panel, value  = 0, precision=3, size=(95, -1))
        self.xas_step = FloatCtrl(panel, value  = 0, precision=3, size=(95, -1))
        self.xas_pre1 = FloatCtrl(panel, value=-200, precision=1, size=(95, -1))
        self.xas_pre2 = FloatCtrl(panel, value= -30, precision=1, size=(95, -1))
        self.xas_nor1 = FloatCtrl(panel, value=  30, precision=1, size=(95, -1))
        self.xas_nor2 = FloatCtrl(panel, value= 300, precision=1, size=(95, -1))
        self.xas_vict = add_choice(panel, size=(50, -1), choices=('0', '1', '2', '3'))
        self.xas_nnor = add_choice(panel, size=(50, -1), choices=('0', '1', '2', '3'))
        self.xas_vict.SetSelection(1)
        self.xas_nnor.SetSelection(2)
        sizer = wx.GridBagSizer(10, 4)

        sizer.Add(SimpleText(p, 'Plot XAS as: '),         (0, 0), (1, 1), LCEN)
        sizer.Add(SimpleText(p, 'E0 : '),                 (1, 0), (1, 1), LCEN)
        sizer.Add(SimpleText(p, 'Edge Step: '),           (2, 0), (1, 1), LCEN)
        sizer.Add(SimpleText(p, 'Pre-edge range: '),      (3, 0), (1, 1), LCEN)
        sizer.Add(SimpleText(p, 'Normalization range: '), (4, 0), (1, 1), LCEN)

        sizer.Add(self.xas_op,                 (0, 1), (1, 1), LCEN)
        sizer.Add(self.xas_e0,                 (1, 1), (1, 1), LCEN)
        sizer.Add(self.xas_step,               (2, 1), (1, 1), LCEN)
        sizer.Add(self.xas_pre1,               (3, 1), (1, 1), LCEN)
        sizer.Add(SimpleText(p, ':'),          (3, 2), (1, 1), LCEN)
        sizer.Add(self.xas_pre2,               (3, 3), (1, 1), LCEN)
        sizer.Add(self.xas_nor1,               (4, 1), (1, 1), LCEN)
        sizer.Add(SimpleText(p, ':'),          (4, 2), (1, 1), LCEN)
        sizer.Add(self.xas_nor2,               (4, 3), (1, 1), LCEN)

        sizer.Add(self.xas_autoe0,             (1, 2), (1, 2), LCEN)
        sizer.Add(self.xas_autostep,           (2, 2), (1, 2), LCEN)

        sizer.Add(SimpleText(p, 'Victoreen:'), (3, 4), (1, 1), LCEN)
        sizer.Add(self.xas_vict,               (3, 5), (1, 1), LCEN)
        sizer.Add(SimpleText(p, 'PolyOrder:'), (4, 4), (1, 1), LCEN)
        sizer.Add(self.xas_nnor,               (4, 5), (1, 1), LCEN)

        pack(panel, sizer)
        return panel

    def onFitPeak(self, evt=None):
        gname = self.groupname
        if self.dtcorr.IsChecked():
            print 'fit needs to dt correct!'

        dtext = []
        model = self.fit_model.GetStringSelection().lower()
        dtext.append('Fit Model: %s' % model)
        bkg =  self.fit_bkg.GetStringSelection()
        if bkg == 'None':
            bkg = None
        if bkg is None:
            dtext.append('No Background')
        else:
            dtext.append('Background: %s' % bkg)

        step = self.fit_step.GetStringSelection().lower()
        if model in ('step', 'rectangle'):
            dtext.append('Step form: %s' % step)
        lgroup =  getattr(self.larch.symtable, gname)
        x = lgroup._x1_
        y = lgroup._y1_
        pgroup = fit_peak(x, y, model, background=bkg, step=step,
                          _larch=self.larch)
        text = fit_report(pgroup.params, _larch=self.larch)
        dtext.append('Parameters: ')
        for pname in dir(pgroup.params):
            par = getattr(pgroup.params, pname)
            if isParameter(par):
                ptxt = "    %s= %.4f" % (par.name, par.value)
                if (hasattr(par, 'stderr') and par.stderr is not None):
                    ptxt = "%s(%.4f)" % (ptxt, par.stderr)
                dtext.append(ptxt)

        dtext = '\n'.join(dtext)
        # plotframe = self.get_plotwindow()
        # plotframe.oplot(x, pgroup.fit, label='fit (%s)' % model)
        text = fit_report(pgroup.params, _larch=self.larch)
        self.fit_report.SetLabel(dtext)

    def xas_process(self, gname, plotopts):
        """ process (pre-edge/normalize) XAS data from XAS form, overwriting
        larch group '_y1_' attribute to be plotted
        """
        print 'Process XAS ', gname
        out = self.xas_op.GetStringSelection().lower() # raw, pre, norm, flat
        if out.startswith('raw'):
            return plotopts

        preopts = {'group': gname, 'e0': None, 'step': None}

        lgroup = getattr(self.larch.symtable, gname)

        if self.dtcorr.IsChecked():
            print 'need to dt correct!'

        if not self.xas_autoe0.IsChecked():
            xmin, xmax = min(lgroup._x1_),  max(lgroup._x1_)
            e0 = self.xas_e0.GetValue()
            if e0 < xmax and e0 > xmin:
                preopts['e0'] = e0

        if not self.xas_autostep.IsChecked():
            preopts['step'] = self.xas_step.GetValue()

        preopts['pre1']  = self.xas_pre1.GetValue()
        preopts['pre2']  = self.xas_pre2.GetValue()
        preopts['norm1'] = self.xas_nor1.GetValue()
        preopts['norm2'] = self.xas_nor2.GetValue()

        preopts['nvict'] = self.xas_vict.GetSelection()
        preopts['nnorm'] = self.xas_nnor.GetSelection()

        preopts = ", ".join(["%s=%s" %(k, v) for k,v in preopts.items()])
        preedge_cmd = "pre_edge(%s._x1_, %s._y1_, %s)" % (gname, gname, preopts)

        self.larch(preedge_cmd)

        self.xas_e0.SetValue(lgroup.e0)
        self.xas_step.SetValue(lgroup.edge_step)

        if out.startswith('pre'):
            self.larch('%s._y1_ = %s.norm * %s.edge_step' % (gname, gname, gname))
        elif out.startswith('norm'):
            self.larch('%s._y1_ = %s.norm' % (gname, gname))
        elif out.startswith('flat'):
            self.larch('%s._y1_ = %s.flat' % (gname, gname))

        return plotopts

    def init_larch(self):
        t0 = time.time()
        from larch.wxlib import inputhook
        self.larch = Interpreter()
        self.larch.symtable.set_symbol('_sys.wx.wxapp', wx.GetApp())
        self.larch.symtable.set_symbol('_sys.wx.parent', self)
        self.larch('%s = group(filename="%s")' % (SCANGROUP, CURSCAN))
        self.lgroup =  getattr(self.larch.symtable, SCANGROUP)
        self.SetStatusText('ready')
        self.title.SetLabel('')

    def write_message(self, s, panel=0):
        """write a message to the Status Bar"""
        self.SetStatusText(s, panel)

    def get_plotwindow(self, new=False, **kws):
        pframe = None
        if not new:
            while pframe is None:
                try:
                    pframe = self.plotters.pop()
                    pframe.Show()
                    pframe.Raise()
                except IndexError:
                    pframe = None
                    break
                except PyDeadObjectError:
                    pframe = None

        if pframe is None:
            pframe = PlotFrame()
            pframe.Show()
            pframe.Raise()

        self.plotters.append(pframe)

        return pframe

    def onYchoice(self, evt=None, side='left'):
        print 'onYchoice '
        self.onPlot()

    def onPlot(self, evt=None, opt='new old', npts=None):
        # 'win new', 'New Window'),
        # 'win old',  'Old Window'),
        # 'over left', 'Left Axis'),
        # 'over right', 'Right Axis')):
        # 'update left',  from scan

        optwords = opt.split()
        # plotframe = self.get_plotwindow(new=('new' in optwords[1]))
        # plotcmd = plotframe.plot
        plotcmd = self.plotpanel.plot

        optwords = opt.split()
        side = 'left'
        update = False
        if optwords[0] == 'over':
            side = optwords[1]
            plotcmd = self.plotpanel.oplot
        elif optwords[0] == 'update'  and npts > 4:
            plotcmd = self.plotpanel.update_line
            update = True

        popts = {'side': side, 'labelfontsize': 8}

        ix = self.xarr.GetSelection()
        x  = self.xarr.GetStringSelection()

        try:
            gname = self.groupname
            lgroup = getattr(self.larch.symtable, gname)
        except:
            gname = SCANGROUP
            lgroup = getattr(self.larch.symtable, gname)

        xfmt = "%s._x1_ = %s"
        yfmt = "%s._y1_ = %s((%s %s %s) %s (%s))"

        popts['xlabel'] = x
        try:
            xunits = lgroup.array_units[ix]
            popts['xlabel'] = '%s (%s)' % (xlabel, xunits)
        except:
            pass

        opl1 = self.yops[0][0].GetStringSelection()
        opl2 = self.yops[0][1].GetStringSelection()
        opl3 = self.yops[0][2].GetStringSelection()

        yl1 = self.yarr[0][0].GetStringSelection()
        yl2 = self.yarr[0][1].GetStringSelection()
        yl3 = self.yarr[0][2].GetStringSelection()

        opr1 = self.yops[1][0].GetStringSelection()
        opr2 = self.yops[1][1].GetStringSelection()
        opr3 = self.yops[1][2].GetStringSelection()

        yr1 = self.yarr[1][0].GetStringSelection()
        yr2 = self.yarr[1][1].GetStringSelection()
        yr3 = self.yarr[1][2].GetStringSelection()

        ylabel = yl1

        if yl2 == '':
            yl2, opl2 = '1', '*'
        else:
            ylabel = "%s%s%s" % (ylabel, opl2, yl2)
        if yl3 == '':
            yl3, opl3 = '1', '*'
        else:
            ylabel = "(%s)%s%s" % (ylabel, opl3, yl3)

        if opl1 != '':
            ylabel = "%s(%s)" % (opl1, ylabel)

        if yl1 not in ('0', '1'): yl1 = "%s.%s" % (gname, yl1)
        if yl2 not in ('0', '1'): yl2 = "%s.%s" % (gname, yl2)
        if yl3 not in ('0', '1'): yl3 = "%s.%s" % (gname, yl3)
        if x  not in ('0', '1'):  x = "%s.%s" % (gname,  x)

        self.larch(xfmt % (gname, x))
        self.larch(yfmt % (gname, opl1, yl1, opl2, yl2, opl3, yl3))

        # print 'Group X ... ', len(lgroup._x1_), lgroup._x1_
        # print 'Group Y ... ', len(lgroup._y1_), lgroup._y1_

        try:
            npts = min(len(lgroup._x1_), len(lgroup._y1_))
        except AttributeError:
            return

        lgroup._x1_ = np.array( lgroup._x1_[:npts])
        lgroup._y1_ = np.array( lgroup._y1_[:npts])


        path, fname = os.path.split(lgroup.filename)
        popts['label'] = "%s: %s" % (fname, ylabel)
        if side == 'right':
            popts['y2label'] = ylabel
        else:
            popts['ylabel'] = ylabel

        if plotcmd == self.plotpanel.plot:
            popts['title'] = fname

        # XAFS Processing!
        #if (self.nb.GetCurrentPage() == self.xas_panel):
        #    popts = self.xas_process(gname, popts)

        if update:
            self.plotpanel.set_xlabel(popts['xlabel'])
            self.plotpanel.set_ylabel(popts['ylabel'])

            plotcmd(0, lgroup._x1_, lgroup._y1_, draw=True,
                        update_limits=True) # ((npts < 5) or (npts % 5 == 0)))

            self.plotpanel.set_xylims((
                min(lgroup._x1_), max(lgroup._x1_),
                min(lgroup._y1_), max(lgroup._y1_)))

        else:
            plotcmd(lgroup._x1_, lgroup._y1_, **popts)
            self.plotpanel.canvas.draw()

    def createMenus(self):
        # ppnl = self.plotpanel
        self.menubar = wx.MenuBar()
        #
        fmenu = wx.Menu()
        pmenu = wx.Menu()
        fmenu.AppendSeparator()
        add_menu(self, fmenu, "&Quit\tCtrl+Q", "Quit program", self.onClose)

        self.menubar.Append(fmenu, "&File")

        fmenu.AppendSeparator()
        add_menu(self, fmenu, "&Copy\tCtrl+C",  "Copy to Clipboard", self.onClipboard)
        add_menu(self, fmenu, "&Save\tCtrl+S", "Save Figure",   self.onSaveFig)
        add_menu(self, fmenu, "&Print\tCtrl+P", "Print Figure", self.onPrint)
        add_menu(self, fmenu, "Page Setup", "Print Page Setup", self.onPrintSetup)
        add_menu(self, fmenu, "Preview", "Print Preview",       self.onPrintPreview)
        #
        
        add_menu(self, pmenu, "Configure\tCtrl+K",
                 "Configure Plot", self.onConfigurePlot)
        add_menu(self, pmenu, "Unzoom\tCtrl+Z", "Unzoom Plot", self.onUnzoom)
        pmenu.AppendSeparator()
        add_menu(self, pmenu, "Toggle Legend\tCtrl+L",
                 "Toggle Legend on Plot", self.onToggleLegend)
        add_menu(self, pmenu, "Toggle Grid\tCtrl+G",
                 "Toggle Grid on Plot", self.onToggleGrid)

        self.menubar.Append(pmenu, "Plot Options")
        self.SetMenuBar(self.menubar)

    def onClipboard(self, evt=None):
        self.plotpanel.canvas.Copy_to_Clipboard(evt)
       
    def onSaveFig(self, evt=None):
        self.plotpanel.save_figure(event=evt,
                                   transparent=True, dpi=300)
        
    def onPrint(self, evt=None):
        self.plotpanel.Print(evet)

    def onPrintSetup(self, evt=None):
        self.plotpanel.PrintSetup(evt)

    def onPrintPreview(self, evt=None):
        self.plotpanel.PrintPreview(evt)

    def onConfigurePlot(self, evt=None):
        self.plotpanel.configure(evt)

    def onUnzoom(self, evt=None):
        self.plotpanel.unzoom(evt)

    def onToggleLegend(self, evt=None):
        self.plotpanel.toggle_legend(evt)

    def onToggleGrid(self, evt=None):
        self.plotpanel.toggle_grid(evt)

    def onAbout(self,evt):
        dlg = wx.MessageDialog(self, self._about,"About Epics StepScan",
                               wx.OK | wx.ICON_INFORMATION)
        dlg.ShowModal()
        dlg.Destroy()

    def onClose(self,evt):
        for obj in self.plotters:
            try:
                obj.Destroy()
            except:
                pass
        for nam in dir(self.larch.symtable._sys.wx):
            obj = getattr(self.larch.symtable._sys.wx, nam)
            del obj

        self.Destroy()

class ScanViewerApp(wx.App, wx.lib.mixins.inspection.InspectionMixin):
    def __init__(self, dbname=None, server='sqlite', host=None,
                 port=None, user=None, password=None, create=True, **kws):

        self.db_opts = dict(dbname=dbname, server=server, host=host,
                            port=port, create=create, user=user,
                            password=password)
        self.db_opts.update(kws)
        wx.App.__init__(self)

    def OnInit(self):
        self.Init()
        frame = ScanViewerFrame(**self.db_opts)
        frame.Show()
        self.SetTopWindow(frame)
        return True

if __name__ == "__main__":
    ViewerApp().MainLoop()
