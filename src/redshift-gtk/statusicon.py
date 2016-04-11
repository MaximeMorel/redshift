# statusicon.py -- GUI status icon source
# This file is part of Redshift.

# Redshift is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

# Redshift is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.

# You should have received a copy of the GNU General Public License
# along with Redshift.  If not, see <http://www.gnu.org/licenses/>.

# Copyright (c) 2013-2014  Jon Lund Steffensen <jonlst@gmail.com>


'''GUI status icon for Redshift.

The run method will try to start an appindicator for Redshift. If the
appindicator module isn't present it will fall back to a GTK status icon.
'''

import sys, os
import fcntl
import signal
import re
import gettext
import dbus
from dbus.mainloop.glib import DBusGMainLoop

import gi
gi.require_version('Gtk', '3.0')

from gi.repository import Gtk, Gdk, GLib, GObject

try:
    gi.require_version('AppIndicator3', '0.1')
    from gi.repository import AppIndicator3 as appindicator
except (ImportError, ValueError):
    appindicator = None

from . import defs
from . import utils

_ = gettext.gettext

def on_dbus_reply(arg):
    None

def on_dbus_error(err):
    print('on_dbus_error:', err)

class RedshiftController(GObject.GObject):
    '''A GObject wrapper around the child process'''

    __gsignals__ = {
        'inhibit-changed': (GObject.SIGNAL_RUN_FIRST, None, (bool,)),
        'temperature-changed': (GObject.SIGNAL_RUN_FIRST, None, (int, int)),
        'period-changed': (GObject.SIGNAL_RUN_FIRST, None, (str,)),
        'location-changed': (GObject.SIGNAL_RUN_FIRST, None, (float, float)),
        'error-occured': (GObject.SIGNAL_RUN_FIRST, None, (str,)),
        'brightness-changed': (GObject.SIGNAL_RUN_FIRST, None, (int, int)),
        }

    def __init__(self, args):
        '''Initialize controller and start child process

        The parameter args is a list of command line arguments to pass on to
        the child process. The "-v" argument is automatically added.'''

        GObject.GObject.__init__(self)

        # Initialize state variables
        self._inhibited = False
        self._temperature = 0
        self._temperature_offset = 0
        self._period = 'Unknown'
        self._location = (0.0, 0.0)
        self._brightness = 100
        self._brightness_offset = 0

        # Start redshift with arguments
        args.insert(0, os.path.join(defs.BINDIR, 'redshift'))
        if '-v' not in args:
            args.insert(1, '-v')

        # Start child process with C locale so we can parse the output
        env = os.environ.copy()
        env['LANG'] = env['LANGUAGE'] = env['LC_ALL'] = env['LC_MESSAGES'] = 'C'
        self._process = GLib.spawn_async(args, envp=['{}={}'.format(k,v) for k, v in env.items()],
                                         flags=GLib.SPAWN_DO_NOT_REAP_CHILD,
                                         standard_output=True, standard_error=True)

        # Wrap remaining contructor in try..except to avoid that the child
        # process is not closed properly.
        try:
            # Handle child input
            # The buffer is encapsulated in a class so we
            # can pass an instance to the child callback.
            class InputBuffer(object):
                buf = ''

            self._input_buffer = InputBuffer()
            self._error_buffer = InputBuffer()
            self._errors = ''

            # Set non blocking
            fcntl.fcntl(self._process[2], fcntl.F_SETFL,
                        fcntl.fcntl(self._process[2], fcntl.F_GETFL) | os.O_NONBLOCK)

            # Add watch on child process
            GLib.child_watch_add(GLib.PRIORITY_DEFAULT, self._process[0], self._child_cb)
            GLib.io_add_watch(self._process[2], GLib.PRIORITY_DEFAULT, GLib.IO_IN,
                              self._child_data_cb, (True, self._input_buffer))
            GLib.io_add_watch(self._process[3], GLib.PRIORITY_DEFAULT, GLib.IO_IN,
                              self._child_data_cb, (False, self._error_buffer))

            # Signal handler to relay USR1 signal to redshift process
            def relay_signal_handler(signal):
                os.kill(self._process[0], signal)
                return True

            GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGUSR1,
                                 relay_signal_handler, signal.SIGUSR1)
        except:
            self.termwait()
            raise

    @property
    def inhibited(self):
        '''Current inhibition state'''
        return self._inhibited

    @property
    def temperature(self):
        '''Current screen temperature'''
        return self._temperature

    @property
    def temperature_offset(self):
        '''Current screen temperature offset'''
        return self._temperature_offset

    @property
    def brightness(self):
        '''Current screen brightness'''
        return self._brightness

    @property
    def brightness_offset(self):
        '''Current screen brightness_offset'''
        return self._brightness_offset

    @property
    def period(self):
        '''Current period of day'''
        return self._period

    @property
    def location(self):
        '''Current location'''
        return self._location

    def set_inhibit(self, inhibit):
        '''Set inhibition state'''
        if inhibit != self._inhibited:
            self._child_toggle_inhibit()

    def _child_signal(self, sg):
        """Send signal to child process."""
        os.kill(self._process[0], sg)

    def _child_toggle_inhibit(self):
        '''Sends a request to the child process to toggle state'''
        self._child_signal(signal.SIGUSR1)

    def _child_cb(self, pid, status, data=None):
        '''Called when the child process exists'''

        # Empty stdout and stderr
        for f in (self._process[2], self._process[3]):
            while True:
                buf = os.read(f, 256).decode('utf-8')
                if buf == '':
                    break
                if f == self._process[3]: # stderr
                    self._errors += buf

        # Check exit status of child
        report_errors = False
        try:
            GLib.spawn_check_exit_status(status)
        except GLib.GError:
            self.emit('error-occured', self._errors)

        GLib.spawn_close_pid(self._process[0])
        Gtk.main_quit()

    def _child_key_change_cb(self, key, value):
        '''Called when the child process reports a change of internal state'''

        def parse_coord(s):
            '''Parse coordinate like `42.0 N` or `91.5 W`'''
            v, d = s.split(' ')
            return float(v) * (1 if d in 'NE' else -1)

        if key == 'Status':
            new_inhibited = value != 'Enabled'
            if new_inhibited != self._inhibited:
                self._inhibited = new_inhibited
                self.emit('inhibit-changed', new_inhibited)
        elif key == 'Color temperature':
            new_temperature = int(value.rstrip('K'), 10)
            if new_temperature != self._temperature:
                self._temperature = new_temperature
                self.emit('temperature-changed', new_temperature, self._temperature_offset)
        elif key == 'Brightness':
            new_brightness = int(float(value) * 100)
            if new_brightness != self._brightness:
                self._brightness = new_brightness
                self.emit('brightness-changed', new_brightness, self._brightness_offset)
        elif key == 'Period':
            new_period = value
            if new_period != self._period:
                self._period = new_period
                self.emit('period-changed', new_period)
        elif key == 'Location':
            new_location = tuple(parse_coord(x) for x in value.split(', '))
            if new_location != self._location:
                self._location = new_location
                self.emit('location-changed', *new_location)

    def _child_stdout_line_cb(self, line):
        '''Called when the child process outputs a line to stdout'''
        if line:
            m = re.match(r'([\w ]+): (.+)', line)
            if m:
                key = m.group(1)
                value = m.group(2)
                self._child_key_change_cb(key, value)

    def _child_data_cb(self, f, cond, data):
        '''Called when the child process has new data on stdout/stderr'''

        stdout, ib = data
        ib.buf += os.read(f, 256).decode('utf-8')

        # Split input at line break
        while True:
            first, sep, last = ib.buf.partition('\n')
            if sep == '':
                break
            ib.buf = last
            if stdout:
                self._child_stdout_line_cb(first)
            else:
                self._errors += first + '\n'

        return True

    def terminate_child(self):
        """Send SIGINT to child process."""
        self._child_signal(signal.SIGINT)

    def kill_child(self):
        """Send SIGKILL to child process."""
        self._child_signal(signal.SIGKILL)


class RedshiftStatusIcon(object):
    '''The status icon tracking the RedshiftController'''

    def __init__(self, controller):
        '''Creates a new instance of the status icon'''

        self._controller = controller

        if appindicator:
            # Create indicator
            self.indicator = appindicator.Indicator.new('redshift',
                                                        'redshift-status-on',
                                                        appindicator.IndicatorCategory.APPLICATION_STATUS)
            self.indicator.set_status(appindicator.IndicatorStatus.ACTIVE)
        else:
            # Create status icon
            self.status_icon = Gtk.StatusIcon()
            self.status_icon.set_from_icon_name('redshift-status-on')
            self.status_icon.set_tooltip_text('Redshift')

        # Create popup menu
        self.status_menu = Gtk.Menu()

        # Add toggle action
        self.toggle_item = Gtk.CheckMenuItem.new_with_label(_('Enabled'))
        self.toggle_item.connect('activate', self.toggle_item_cb)
        self.status_menu.append(self.toggle_item)

        # Add suspend menu
        suspend_menu_item = Gtk.MenuItem.new_with_label(_('Suspend for'))
        suspend_menu = Gtk.Menu()
        for minutes, label in [(30, _('30 minutes')),
                               (60, _('1 hour')),
                               (120, _('2 hours'))]:
            suspend_item = Gtk.MenuItem.new_with_label(label)
            suspend_item.connect('activate', self.suspend_cb, minutes)
            suspend_menu.append(suspend_item)
        suspend_menu_item.set_submenu(suspend_menu)
        self.status_menu.append(suspend_menu_item)

        # Add autostart option
        autostart_item = Gtk.CheckMenuItem.new_with_label(_('Autostart'))
        try:
            autostart_item.set_active(utils.get_autostart())
        except IOError as strerror:
            print(strerror)
            autostart_item.set_property('sensitive', False)
        else:
            autostart_item.connect('toggled', self.autostart_cb)
        finally:
            self.status_menu.append(autostart_item)

        # Add info action
        info_item = Gtk.MenuItem.new_with_label(_('Info'))
        info_item.connect('activate', self.show_info_cb)
        self.status_menu.append(info_item)

        # Add quit action
        quit_item = Gtk.ImageMenuItem.new_with_label(_('Quit'))
        quit_item.connect('activate', self.destroy_cb)
        self.status_menu.append(quit_item)

        # Create info dialog
        self.info_dialog = Gtk.Dialog()
        self.info_dialog.set_title(_('Info'))
        self.info_dialog.add_button(_('Close'), Gtk.ButtonsType.CLOSE)
        self.info_dialog.set_resizable(False)
        self.info_dialog.set_property('border-width', 6)

        self.status_label = Gtk.Label()
        self.status_label.set_alignment(0.0, 0.5)
        self.status_label.set_padding(6, 6)
        self.info_dialog.get_content_area().pack_start(self.status_label, True, True, 0)
        self.status_label.show()

        self.location_label = Gtk.Label()
        self.location_label.set_alignment(0.0, 0.5)
        self.location_label.set_padding(6, 6)
        self.info_dialog.get_content_area().pack_start(self.location_label, True, True, 0)
        self.location_label.show()

        self.temperature_label = Gtk.Label()
        self.temperature_label.set_alignment(0.0, 0.5)
        self.temperature_label.set_padding(6, 6)
        self.temperature_label.set_width_chars(40);
        self.info_dialog.get_content_area().pack_start(self.temperature_label, True, True, 0)
        self.temperature_label.show()

        self.temperature_scale = Gtk.HScale()
        self.temperature_scale.set_range(-5000, 5000)
        self.temperature_scale.set_digits(0)
        self.info_dialog.get_content_area().pack_start(self.temperature_scale, True, True, 0)
        self.temperature_scale.connect('change-value', self.temperature_scale_cb)
        self.temperature_scale.show()

        self.brightness_label = Gtk.Label()
        self.brightness_label.set_alignment(0.0, 0.5)
        self.brightness_label.set_padding(6, 6)
        self.info_dialog.get_content_area().pack_start(self.brightness_label, True, True, 0)
        self.brightness_label.show()

        self.brightness_scale = Gtk.HScale()
        self.brightness_scale.set_range(-100, 100)
        self.brightness_scale.set_digits(0)
        self.info_dialog.get_content_area().pack_start(self.brightness_scale, True, True, 0)
        self.brightness_scale.connect('change-value', self.brightness_scale_cb)
        self.brightness_scale.show()

        self.period_label = Gtk.Label()
        self.period_label.set_alignment(0.0, 0.5)
        self.period_label.set_padding(6, 6)
        self.info_dialog.get_content_area().pack_start(self.period_label, True, True, 0)
        self.period_label.show()

        self.info_dialog.connect('response', self.response_info_cb)

        # Setup signals to property changes
        self._controller.connect('inhibit-changed', self.inhibit_change_cb)
        self._controller.connect('period-changed', self.period_change_cb)
        self._controller.connect('temperature-changed', self.temperature_change_cb)
        self._controller.connect('location-changed', self.location_change_cb)
        self._controller.connect('error-occured', self.error_occured_cb)
        self._controller.connect('brightness-changed', self.brightness_change_cb)

        # Set info box text
        self.change_inhibited(self._controller.inhibited)
        self.change_period(self._controller.period)
        self.change_temperature(self._controller.temperature, self._controller.temperature_offset)
        self.change_location(self._controller.location)
        self.change_brightness(self._controller.brightness, self._controller.brightness_offset)

        if appindicator:
            self.status_menu.show_all()

            # Set the menu
            self.indicator.set_menu(self.status_menu)
        else:
            # Connect signals for status icon and show
            self.status_icon.connect('activate', self.toggle_cb)
            self.status_icon.connect('popup-menu', self.popup_menu_cb)
            self.status_icon.set_visible(True)

        # Initialize suspend timer
        self.suspend_timer = None
        self.dbus_loop = DBusGMainLoop()
        self.dbusInit = False

    def set_dbus_value_offset(self, type, val):
        """type == 0 means temperature, type == 1 means brightness"""
        if self._controller.inhibited == False:
            if self.dbusInit == True:
                try:
                    #print("setValueOffsetMethod ", type, val)
                    self.setValueOffsetMethod(type, val, reply_handler=on_dbus_reply, error_handler=on_dbus_error)
                except BaseException as err:
                    print("DBus call method error: ", err)
                    self.dbusInit = False
                    pass
            else:
                try:
                    self.session_bus = dbus.SessionBus(mainloop=self.dbus_loop)
                    self.dbusProxy = self.session_bus.get_object('dk.jonls.redshift', '/dk/jonls/redshift', introspect=False)
                    self.setValueOffsetMethod = self.dbusProxy.get_dbus_method('setValueOffset', 'dk.jonls.redshift')
                    self.dbusInit = True
                    print("Open DBus interface")
                except dbus.DBusException as err:
                    print("DBus error: ", err)
                    pass

    def temperature_scale_cb(self, range, scroll, value):
        if value < -5000:
            value = -5000
        if value > 5000:
            value = 5000
        self._controller._temperature_offset = int(value)
        self._controller.emit('temperature-changed', self._controller.temperature, self._controller.temperature_offset)
        self.set_dbus_value_offset(0, self._controller._temperature_offset)

    def brightness_scale_cb(self, range, scroll, value):
        if value < -100:
            value = -100
        if value > 100:
            value = 100
        self._controller._brightness_offset = int(value)
        self._controller.emit('brightness-changed', self._controller.brightness, self._controller.brightness_offset)
        self.set_dbus_value_offset(1, self._controller._brightness_offset)

    def remove_suspend_timer(self):
        '''Disable any previously set suspend timer'''
        if self.suspend_timer is not None:
            GLib.source_remove(self.suspend_timer)
            self.suspend_timer = None

    def suspend_cb(self, item, minutes):
        '''Callback that handles activation of a suspend timer

        The minutes parameter is the number of minutes to suspend. Even if redshift
        is not disabled when called, it will still set a suspend timer and
        reactive redshift when the timer is up.'''

        # Inhibit
        self._controller.set_inhibit(True)

        # If "suspend" is clicked while redshift is disabled, we reenable
        # it after the last selected timespan is over.
        self.remove_suspend_timer()

        # If redshift was already disabled we reenable it nonetheless.
        self.suspend_timer = GLib.timeout_add_seconds(minutes * 60, self.reenable_cb)

    def reenable_cb(self):
        '''Callback to reenable redshift when a suspend timer expires'''
        self._controller.set_inhibit(False)

    def popup_menu_cb(self, widget, button, time, data=None):
        '''Callback when the popup menu on the status icon has to open'''
        self.status_menu.show_all()
        self.status_menu.popup(None, None, Gtk.StatusIcon.position_menu,
                               self.status_icon, button, time)

    def toggle_cb(self, widget, data=None):
        '''Callback when a request to toggle redshift was made'''
        self.remove_suspend_timer()
        self._controller.set_inhibit(not self._controller.inhibited)

    def toggle_item_cb(self, widget, data=None):
        '''Callback then a request to toggle redshift was made from a toggle item

        This ensures that the state of redshift is synchronised with
        the toggle state of the widget (e.g. Gtk.CheckMenuItem).'''

        active = not self._controller.inhibited
        if active != widget.get_active():
            self.remove_suspend_timer()
            self._controller.set_inhibit(not self._controller.inhibited)

    # Info dialog callbacks
    def show_info_cb(self, widget, data=None):
        '''Callback when the info dialog should be presented'''
        self.info_dialog.show()

    def response_info_cb(self, widget, data=None):
        '''Callback when a button in the info dialog was activated'''
        self.info_dialog.hide()

    def update_status_icon(self):
        '''Update the status icon according to the internally recorded state

        This should be called whenever the internally recorded state
        might have changed.'''

        # Update status icon
        if appindicator:
            if not self._controller.inhibited:
                self.indicator.set_icon('redshift-status-on')
            else:
                self.indicator.set_icon('redshift-status-off')
        else:
            if not self._controller.inhibited:
                self.status_icon.set_from_icon_name('redshift-status-on')
            else:
                self.status_icon.set_from_icon_name('redshift-status-off')

    # State update functions
    def inhibit_change_cb(self, controller, inhibit):
        '''Callback when controller changes inhibition status'''
        self.change_inhibited(inhibit)

    def period_change_cb(self, controller, period):
        '''Callback when controller changes period'''
        self.change_period(period)

    def temperature_change_cb(self, controller, temperature, temperature_offset):
        '''Callback when controller changes temperature'''
        self.change_temperature(temperature, temperature_offset)

    def brightness_change_cb(self, controller, brightness, brightness_offset):
        '''Callback when controller changes brightness'''
        self.change_brightness(brightness, brightness_offset)

    def location_change_cb(self, controller, lat, lon):
        '''Callback when controlled changes location'''
        self.change_location((lat, lon))

    def error_occured_cb(self, controller, error):
        '''Callback when an error occurs in the controller'''
        error_dialog = Gtk.MessageDialog(None, Gtk.DialogFlags.MODAL, Gtk.MessageType.ERROR,
                                         Gtk.ButtonsType.CLOSE, '')
        error_dialog.set_markup('<b>Failed to run Redshift</b>\n<i>' + error + '</i>')
        error_dialog.run()

        # Quit when the model dialog is closed
        sys.exit(-1)

    # Update interface
    def change_inhibited(self, inhibited):
        '''Change interface to new inhibition status'''
        self.update_status_icon()
        self.toggle_item.set_active(not inhibited)
        self.status_label.set_markup(_('<b>Status:</b> {}').format(_('Disabled') if inhibited else _('Enabled')))

    def change_temperature(self, temperature, temperature_offset):
        '''Change interface to new temperature'''
        sign = ('', '+')[temperature_offset >= 0]
        self.temperature_label.set_markup('<b>{}:</b> {}K ({}{}K)'.format(_('Color temperature'), temperature, sign, temperature_offset))

    def change_brightness(self, brightness, brightness_offset):
        '''Change interface to new brightness'''
        sign = ('', '+')[brightness_offset >= 0]
        self.brightness_label.set_markup('<b>{}:</b> {}% ({}{}%)'.format(_('Brightness'), brightness, sign, brightness_offset))

    def change_period(self, period):
        '''Change interface to new period'''
        self.period_label.set_markup('<b>{}:</b> {}'.format(_('Period'), period))

    def change_location(self, location):
        '''Change interface to new location'''
        self.location_label.set_markup('<b>{}:</b> {}, {}'.format(_('Location'), *location))


    def autostart_cb(self, widget, data=None):
        '''Callback when a request to toggle autostart is made'''
        utils.set_autostart(widget.get_active())

    def destroy_cb(self, widget, data=None):
        '''Callback when a request to quit the application is made'''
        if not appindicator:
            self.status_icon.set_visible(False)
        self._controller.terminate_child()
        return False


def run():
    utils.setproctitle('redshift-gtk')

    # Internationalisation
    gettext.bindtextdomain('redshift', defs.LOCALEDIR)
    gettext.textdomain('redshift')

    # Create redshift child process controller
    c = RedshiftController(sys.argv[1:])

    def terminate_child(data=None):
        c.terminate_child()
        return False

    # Install signal handlers
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM,
                         terminate_child, None)
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGINT,
                         terminate_child, None)

    try:
        # Create status icon
        s = RedshiftStatusIcon(c)

        # Run main loop
        Gtk.main()
    except:
        c.kill_child()
        raise
