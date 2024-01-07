# -*- coding: utf-8; indent-tabs-mode: t; tab-width: 4 -*- vim: noet
from __future__ import print_function
from __future__ import with_statement

import math
import os
import re
import shlex
import socket
import struct
import sys
import threading

if sys.version_info[0] <= 2:
	import Tkinter as tk
	import tkFont as tkfont
	# Although Py2.x has 'ttk', support for 2.x is mostly targeted to Win98,
	# where it crashes with TclError (missing tile.tcl), so we don't use it.
	#import ttk
	ttk = None
	from tkSimpleDialog import askstring
	from tkMessageBox import showinfo, showerror
else:
	import tkinter as tk
	import tkinter.font as tkfont
	import tkinter.ttk as ttk
	from tkinter.simpledialog import askstring
	from tkinter.messagebox import showinfo, showerror

#ttk = None
ttkstyle = None
#ttkstyle = "classic"
#ttkstyle = "clam"
#ttkstyle = "default"
ttkprogressbar = True
#ttkprogressbar = False
fontsize = 12

if sys.platform == "win32":
	VER_WIN95C  = (4, 0, 67109975)
	VER_WIN98SE = (4, 10, 67766446)
	VER_WINXP   = (5, 1, 2600)

	# Threaded updates don't work on Windows 98 (4,1,z) -- the program
	# seems to completely wedge up at that point.
	# (Not yet tested: Win2000/ME/NT4)
	winver = sys.getwindowsversion()
	if winver[:3] <= VER_WIN98SE:
		print("disabling threaded updates for Windows %d.%d.%d" % winver[:3])
		threading = None

if sys.platform in ("linux2", "linux"):
	# All default Ttk themes look kind of bad on X11 (clam is okay but I need
	# to figure out how to set less-bland colors for the fillbar).
	#ttkstyle = "classic"
	ttkprogressbar = False

def configpaths(name):
	return [os.path.join(sys.path[0], ".%s" % name),
	        os.path.expanduser("~/.%s" % name),
	        os.path.expanduser("~/.config/%s" % name)]

def loadservers(path):
	# .upslist.conf contains a list of UPS addreses, one 'ups@host' per line, with
	# optional description after the address. An address with only '@host' means
	# an apcupsd server instead of a NUT server.
	servers = []
	with open(path, "r") as fh:
		for line in fh:
			line = line.rstrip()
			if not line:
				continue
			if line.startswith("#"):
				continue
			line = line.split(None, 1)
			upsaddr = line[0]
			upsdesc = line[1] if len(line) >= 2 else None
			servers.append((upsaddr, upsdesc))
	return servers

def tryloadservers(paths):
	for path in paths:
		try:
			return loadservers(path)
		except (OSError, IOError):
			pass
	return []

def writeservers(path, servers):
	with open(path, "a") as fh:
		for addr, desc in servers:
			if desc:
				fh.write("%s\t\t%s\n" % (addr, desc))
			else:
				fh.write("%s\n" % (addr,))

def clamp(x, low, high):
	return min(max(x, low), high)

def hms(seconds):
	t = seconds;	h = t // 3600
	t = t % 3600;	m = t // 60
	t = t % 60;	s = t
	return "%dh %02dm" % (h, m)

def nutstrstatus(vars):
	NUTSTATUS = {
		"ALARM":	"alarm",
		#"OL":		"on line power",
		"OL":		"online",
		"OB":		"on battery",
		"LB":		"battery low",
		"HB":		"battery high",
		"RB":		"replace battery",
		"BYPASS":	"bypass",
		"CAL":		"calibrating",
		"OFF":		"output offline",
		"OVER":		"overload",
		"TRIM":		"trimming",
		"BOOST":	"boosting",
		"FSD":		"forced shutdown",
	}

	flags = vars["ups.status"].split()
	text = []
	color = 0	# 3 for red, 2 for orange, 1 for green

	# Skip announcing the 'OL' flag to make the string shorter
	if "OL" in flags:
		for w in list(flags):
			if w in ("BOOST", "TRIM"):
				# boost/trim already imply 'on line' (sort of)
				flags.remove("OL")
			#elif w not in ("OL", "OB"):
			#	# shorten when used in combination
			#	NUTSTATUS["OL"] = "online"

	# Replace the generic 'ALARM' string
	if "ALARM" in flags:
		alarm = vars["ups.alarm"]
		# Remove the generic nutdrv_qx_voltronic prefix
		alarm = re.sub(r"^UPS warnings: ", "", alarm)
		if alarm in ("BOOST", "TRIM"):
			# Ignore, completely redundant
			flags.remove("ALARM")
		elif alarm == "Warning for Battery replace. Replace battery!":
			# Ignore, 'RB' already present
			flags.remove("ALARM")
		else:
			NUTSTATUS["ALARM"] = "alarm [%s]" % alarm

	# Convert all remaining flags to text
	for w in flags:
		st = NUTSTATUS.get(w, w)
		if w == "CAL":
			color = max(color, 1)
		elif w in ("BOOST", "TRIM"):
			if vars.get("input.voltage"):
				st += " (input %.1fV)" % float(vars["input.voltage"])
			color = max(color, 1)
		elif w == "OB":
			# On battery - red/orange/green based on runtime
			bat = float(vars.get("battery.charge", 50))
			eta = float(vars.get("battery.runtime", 600))
			if eta < 15*60:   color = max(color, 3)
			elif eta < 30*60: color = max(color, 2)
			else:             color = max(color, 1)
		elif w == "RB":
			# Replace Battery - red if under 15 minutes, orange otherwise
			eta = float(vars.get("battery.runtime", 600))
			if eta < 15*60: color = max(color, 3)
			else:           color = max(color, 2)
		elif w in ("ALARM", "OVER", "FSD"):
			# Always red
			color = max(color, 3)
		text.append(st)

	return (", ".join(text)), color

def nutgetpower(vars):
	# Get approximate 'real' power usage in W.
	#
	# VA is apparent power, W is real power (identical in DC, but not in AC)
	#   V * A => W
	#   rms(V) * rms(A) => VA
	#   VA * pf => W
	# ups.power is in VA, ups.realpower is in W

	if "ups.realpower.nominal" in vars:
		# apcupsd reports this only (no output current and no factual voltage;
		# output.voltage always shows inverter voltage even while on bypass)
		maxpowerW = float(vars["ups.realpower.nominal"])
		curload = float(vars["ups.load"])
		realpower = maxpowerW * curload / 100
	elif "ups.power.nominal" in vars:
		# Orvaldi reports ups.power and voltage/current
		# output.voltage * output.current == power.nominal * load * powerfactor
		maxpowerVA = float(vars["ups.power.nominal"])
		curload = float(vars["ups.load"])
		pwrfactor = float(vars["output.powerfactor"])
		realpower = maxpowerVA * curload / 100 * pwrfactor
	elif "output.current" in vars and "output.voltage" in vars:
		# Rank this down because it has low precision (only 0.1V*0.1A, which
		# at low mains voltage jumps by more than ~20W per 0.1A -- whereas the
		# power*load measurement is only ~9W per 1%).
		outcurrent = float(vars["output.current"])
		outvoltage = float(vars["output.voltage"])
		realpower = outcurrent * outvoltage
	else:
		realpower = 0
	return realpower

def tryclose(file):
	if file is not None:
		try:
			file.close()
		except:
			pass

class UpsError(Exception):
	pass

class UpsProtocolError(IOError):
	pass

class Ups:
	PORT = 0
	FMODE = "rw"

	def __init__(self, address):
		self.upsname, _, self.hostname = address.rpartition("@")
		self.hostname = self.hostname or "localhost"
		self.address = "%s@%s" % (self.upsname, self.hostname)
		self.sock = None
		self.stream = None

	def __repr__(self):
		return "Ups(%r)" % self.address

	def connect(self):
		print("connecting", self.hostname)
		# Note: Do not convert gaierror to a fatal error like we do for
		# "unknown UPS", as it occurs when the system is resuming from sleep.
		res = socket.getaddrinfo(self.hostname,
								 self.PORT,
								 socket.AF_UNSPEC,
								 socket.SOCK_STREAM)
		for (af, kind, proto, cname, addr) in res:
			self.sock = socket.socket(af, kind, proto)
			self.sock.settimeout(2.0)
			self.sock.connect(addr)
			self.stream = self.sock.makefile(self.FMODE)
			break

	def tryconnect(self):
		if not self.sock:
			self.connect()

	def close(self):
		self.stream = tryclose(self.stream)
		self.sock = tryclose(self.sock)

class NutUps(Ups):
	PORT = 3493

	def __repr__(self):
		return "NutUps(%r)" % self.address

	def send(self, line):
		self.tryconnect()
		self.stream.write(line + "\n")
		self.stream.flush()

	def recv(self):
		line = self.stream.readline()
		if line:
			return line.rstrip("\r\n")
		else:
			return None

	@staticmethod
	def tokenize(line):
		# There isn't really any formal syntax for quoting or escaping;
		# NUT just directly emits a sprintf("VAR %s %s \"%s\"\n") and the
		# bundled Perl module regexes the quotes away.
		return shlex.split(line)

	def recvone(self):
		line = self.recv()
		if line is None:
			raise UpsProtocolError("End of stream")
		elif not line:
			raise UpsProtocolError("Empty line")
		try:
			words = self.tokenize(line)
		except ValueError:
			e = sys.exc_info(1)
			raise UpsProtocolError("Tokenize error - %s: %r" % (e, line))
		return words

	def recvlist(self):
		items = []
		topic = None
		while True:
			resp = self.recvone()
			if resp[0] == "ERR":
				raise UpsError(*resp[1:])
			elif resp[0] == "BEGIN":
				if topic is not None:
					raise UpsProtocolError("BEGIN in the middle of a list: %r" % (resp,))
				if len(resp) < 3 or resp[1] != "LIST":
					raise UpsProtocolError("Not enough parameters: %r" % (resp,))
				topic = resp[2]
			elif resp[0] == "END":
				if topic is None:
					raise UpsProtocolError("END without BEGIN: %r" % (resp,))
				if len(resp) < 3 or resp[1] != "LIST":
					raise UpsProtocolError("Not enough parameters: %r" % (resp,))
				break
			elif resp[0] == topic:
				items.append(tuple(resp[1:]))
			else:
				raise UpsProtocolError("Unexpected: %r" % (resp,))
		return items

	def listvars(self):
		self.send("LIST VAR %s" % self.upsname)
		resp = self.recvlist()
		vars = {}
		for ups, var, value in resp:
			vars[var] = value
		return vars

	def getvar(self, name):
		self.tryconnect()
		self.send("GET VAR %s %s" % (self.upsname, name))
		resp = self.recvone()
		if resp[0] == "ERR":
			if resp[1] == "VAR-NOT-SUPPORTED":
				raise KeyError(name)
			else:
				raise UpsError(*resp[1:])
		elif resp[0] == "VAR":
			if len(resp) < 4:
				raise UpsProtocolError("Not enough parameters: %r" % (resp,))
			elif resp[1:3] != (self.upsname, name):
				raise UpsProtocolError("Desynchronized: %r; expected: %r" % (resp, name))
			return resp[3]
		else:
			raise UpsProtocolError("Unexpected: %r" % (resp,))

class ApcupsdUps(Ups):
	PORT = 3551
	FMODE = "rwb"

	def __repr__(self):
		return "ApcupsdUps(%r)" % self.address

	def send(self, command):
		self.tryconnect()
		buf = command.encode("utf-8")
		buf = struct.pack(">h", len(buf)) + buf
		self.stream.write(buf)
		self.stream.flush()

	def recvone(self):
		buf = self.stream.read(2)
		length, = struct.unpack(">h", buf)
		buf = self.stream.read(length)
		return buf.decode("utf-8")

	def getstatus(self):
		self.send("status")
		vars = {}
		while True:
			buf = self.recvone()
			if len(buf) == 0:
				break
			key, _, val = buf.partition(": ")
			key = key.rstrip()
			val = val.rstrip("\n")
			if not vars and key != "APC":
				raise UpsProtocolError("Status did not start with 'APC': %r" % [key, val])
			if "END APC" in vars:
				raise UpsProtocolError("Unexpected data after 'END APC': %r" % [key, val])
			vars[key] = val
		if "END APC" not in vars:
			raise UpsProtocolError("Status did not finish with 'END APC': %r" % vars)
		return vars

	def listvars(self):
		intmap = {
			"BATTV":	"battery.voltage",
			"BCHARGE": 	"battery.charge",
			"LINEV":	"input.voltage",
			"LOADPCT":	"ups.load",
			"NOMPOWER":	"ups.realpower.nominal",
		}
		strmap = {
			"UPSNAME":	"ups.id",
		}
		statusmap = {
			"CAL":		"CAL",
			"TRIM":		"TRIM",
			"BOOST":	"BOOST",
			"ONLINE":	"OL",
			"ONBATT":	"OB",
			"OVERLOAD":	"OVER",
			"LOWBATT":	"LB",
			"REPLACEBATT":	"RB",
			# Mappings not yet checked against what NUT would show:
			"NOBATT":	"NOBATT?",
			"COMMLOST":	"COMMLOST?",
			"SELFTEST":	"SELFTEST?",
		}
		avars = self.getstatus()
		nvars = {}
		for akey, aval in avars.items():
			if akey in intmap:
				nvars[intmap[akey]] = float(aval.split()[0])
			elif akey in strmap:
				nvars[strmap[akey]] = aval.strip()
			elif akey == "TIMELEFT":
				aval, unit = aval.split()
				assert unit == "Minutes"
				nvars["battery.runtime"] = float(aval) * 60
			elif akey == "STATUS":
				if aval == "SHUTTING DOWN":
					nval = ["FSD"]
				elif aval == "NETWORK ERROR":
					nval = ["COMMLOST"]
				else:
					nval = [statusmap.get(v, "%s?" % v)
					        for v in aval.split()]
				nvars["ups.status"] = (" ".join(nval) or "UNKNOWN")
		return nvars

class TkCustomWidget:
	def config(self, **kv):
		for key, value in kv.items():
			self[key] = value

	configure = config

	def pack(self, *a, **kw):
		self.outer.pack(*a, **kw)

	def grid(self, *a, **kw):
		self.outer.grid(*a, **kw)

if ttk:
	def cnfpadding(cnf):
		padx = 0
		pady = 0
		if "padx" in cnf:
			padx = cnf["padx"]
			del cnf["padx"]
		if "pady" in cnf:
			pady = cnf["pady"]
			del cnf["pady"]
		if padx or pady:
			cnf["padding"] = (padx, pady, padx, pady)
		return cnf

	class TkFrame(ttk.Frame):
		def __init__(self, parent=None, cnf={}, **kw):
			cnf = tk._cnfmerge((cnf, kw))
			cnf = cnfpadding(cnf)
			ttk.Frame.__init__(self, parent, **cnf)

	class TkLabelFrame(ttk.LabelFrame):
		def __init__(self, parent=None, cnf={}, **kw):
			cnf = tk._cnfmerge((cnf, kw))
			cnf = cnfpadding(cnf)
			ttk.LabelFrame.__init__(self, parent, **cnf)

	class TkLabel(ttk.Label):
		def configstyle(self, fg="", bold=False):
			self.config(foreground=fg)
else:
	class TkLabelFrame(tk.LabelFrame):
		def __init__(self, parent=None, cnf={}, **kw):
			tk.LabelFrame.__init__(self, parent, cnf, **kw)
			# Default is "{MS Sans Serif} 8" on Win98, so we can make it bold
			# while keeping the same face and size. (Latest Tk on Win11 sets
			# this to "TkDefaultFont".)
			if self["font"].startswith("{MS Sans Serif} "):
				self["font"] = "%s bold" % self["font"]

	TkFrame = tk.Frame

	class TkLabel(tk.Label):
		def configstyle(self, fg="", bold=False):
			self.config(fg=fg)

if ttk and ttkprogressbar:
	TkProgressBar = ttk.Progressbar
else:
	class TkProgressBar(TkCustomWidget):
		def __init__(self, parent=None, value=0, length=100):
			global fontsize
			self.value = value
			self.width = length
			self.height = fontsize
			self.outer = tk.Frame(parent, borderwidth=2, relief="sunken", padx=1, pady=1)
			self.bg = tk.Frame(self.outer)
			self.bg.columnconfigure(0, minsize=self.width)
			self.bg.rowconfigure(0, minsize=self.height)
			self.bg.pack()
			self.bar = tk.Frame(self.bg, width=0, height=self.height, bg=self.colorforvalue(0))
			self.bar.grid_propagate(0)
			self.bar.grid(row=0, column=0, sticky=tk.N+tk.S+tk.W)

		def colorforvalue(self, value):
			#return "#994444"
			return "#4a6984" # classic Ttk progress bar color

		def __setitem__(self, key, value):
			if key == "value":
				self.value = clamp(value, 0, 100)
				self.bar.config(width=int(self.width / 100.0 * self.value),
								bg=self.colorforvalue(self.value))
			else:
				raise KeyError(key)

class UpsInfoWidget(TkCustomWidget):
	def _addrow(self, label, central, right=None):
		row = self.numrows; self.numrows += 1
		label = TkLabel(self.frame, text=label)
		if right:
			label.grid(row=row, column=0, sticky=tk.E, padx=2, pady=1)
			central.grid(row=row, column=1, sticky=tk.W)
			right.grid(row=row, column=2, sticky=tk.W, padx=2)
		else:
			label.grid(row=row, column=0, sticky=tk.E, padx=2, pady=1)
			central.grid(row=row, column=1, columnspan=2, sticky=tk.W)

	def __init__(self, parent, ups, title):
		if not title:
			title = "%s on %s" % (ups.upsname, ups.hostname)

		self.ups = ups
		self.title = title
		self.timer = None
		self.valid = True
		self.laststatus = None

		global interval
		self.interval = interval

		self.outer = TkFrame(parent, padx=5, pady=5)
		self.frame = TkLabelFrame(self.outer, padx=5, pady=3)
		self.frame.pack()
		# Reduce relayouting on update, by always giving space for 4 chars
		self.frame.columnconfigure(2, minsize=4*10)
		self.numrows = 0

		#self.server_str = TkLabel(frame, justify=tk.LEFT)
		#self._addrow("UPS:", self.server_str)

		self.status_str = TkLabel(self.frame, justify=tk.LEFT, wraplength=120+4*10)
		self._addrow("Status:", self.status_str)

		self.batt_bar = TkProgressBar(self.frame, length=120)
		self.batt_str = TkLabel(self.frame)
		self._addrow("Battery:", self.batt_bar, self.batt_str)

		self.runeta_str = TkLabel(self.frame)
		self._addrow("Runtime:", self.runeta_str)

		self.load_bar = TkProgressBar(self.frame, length=120)
		self.load_str = TkLabel(self.frame)
		self._addrow("Load:", self.load_bar, self.load_str)

		self.power_str = TkLabel(self.frame)
		self._addrow("Power:", self.power_str)

		self.frame.config(text=self.title)
		#self.server_str.config(text=self.title)
		self.updateclear(text="connecting")

	def softlistvars(self, isretry=False):
		# Avoid polling if no such UPS is configured.
		# (Disabled because configuration might change)
		#if not self.valid:
		#	return None

		try:
			return self.ups.listvars()
		except (OSError, IOError):
			e = sys.exc_info()[1]
			# External errors, usually non-fatal
			print("error (%r): %r" % (self.ups, e))
			self.ups.close()
			if isretry:
				return None
			elif self.laststatus:
				self.updateclear("connection lost (%s)" % self.laststatus)
			else:
				self.updateclear("connection failed")
			return self.softlistvars(isretry=True)
		except UpsError:
			e = sys.exc_info()[1]
			# Errors from UPS daemon, usually fatal
			print("error (%r): %r" % (self.ups, e))
			self.ups.close()
			self.valid = False
			self.updateclear("invalid (%s)" % e.args[0])
			print("giving up on %r" % self.ups)
			return None

	def updateclear(self, text="not connected"):
		self.status_str.config(text=text)
		self.batt_bar.config(value=0)
		self.batt_str.config(state=tk.DISABLED, text="???%")
		self.load_bar.config(value=0)
		self.load_str.config(state=tk.DISABLED, text="???%")
		self.runeta_str.config(state=tk.DISABLED, text="--")
		self.power_str.config(state=tk.DISABLED, text="--")

	def updateonce(self):
		vars = self.softlistvars()
		if not vars:
			return

		batt = float(vars["battery.charge"])
		load = float(vars["ups.load"])
		runeta = float(vars["battery.runtime"])
		if runeta > 3600:
			runeta = round(runeta / 600) * 600		# 10 min. precision
		realpower = nutgetpower(vars)
		realpower = round(realpower / 10) * 10	# 10 W precision
		status = vars["ups.status"].split()
		strstatus, intstatus = nutstrstatus(vars)

		self.laststatus = strstatus
		self.status_str.config(state=tk.NORMAL, text=strstatus)
		self.batt_bar.config(value=int(batt))
		self.batt_str.config(state=tk.NORMAL, text="%.0f%%" % batt)
		self.load_bar.config(value=int(load))
		self.load_str.config(state=tk.NORMAL, text="%.0f%%" % load)
		self.runeta_str.config(state=tk.NORMAL, text="approx. %s" % hms(int(runeta)))
		self.power_str.config(state=tk.NORMAL, text="approx. %dW" % realpower)

		#colors = ["", "green", "#d09000", "#d00000"]
		#if intstatus >= 2:
		#	self.status_str.configstyle(fg=colors[intstatus], bold=True)
		#else:
		#	self.status_str.configstyle(fg=colors[intstatus])

	def updatetimer(self):
		self.updateonce()
		self.timer = root.after(self.interval, self.updatetimer)

	def updatethread(self):
		# XXX: This needs some kind of locking so that if one thread takes
		# more than <interval> to do its thing, we don't end up with two
		# threads being spawned concurrently.
		# if self.thread: self.thread.join()
		self.thread = threading.Thread(target=self.updateonce)
		self.thread.start()
		self.timer = root.after(self.interval, self.updatethread)

# Load configured hosts

confpaths = configpaths("upslist.conf")
if len(sys.argv) > 1:
	servers = [(a, None) for a in sys.argv[1:]]
else:
	servers = tryloadservers(confpaths)
interval = 5*1000

# Initialize Tk

root = tk.Tk()
root.title("UPS status")

if ttk:
	if ttkstyle:
		ttk.Style().theme_use(ttkstyle)

	if fontsize != 12:
		ttk.Style().configure(".", font=("TkDefaultFont", -fontsize))

	# It seems that Ttk has magic for determining the correct family and
	# size of 'TkDefaultFont', such that any change (e.g. weight=BOLD)
	# will break it and no size value is right. Fortunately, we kind of
	# want to make it larger and more prominent anyway.
	#deffont = tkfont.nametofont("TkDefaultFont")
	#boldfont = deffont.copy()
	#boldfont.configure(weight=tkfont.BOLD)
	ttk.Style().configure("TLabelframe.Label", font=("TkDefaultFont", -fontsize, "bold"))

# Show main window

saveservers = False
if not servers:
	answer = askstring("upsmonitor",
					   "No devices found in .upslist.conf\n\nUPS address (name@host):")
	if answer:
		servers.append((answer, None))
		saveservers = True

for addr, desc in servers:
	if addr.startswith("@"):
		ups = ApcupsdUps("apcupsd" + addr)
	elif "@" in addr:
		ups = NutUps(addr)
	else:
		showerror("upsmonitor", "Invalid UPS address '%s'." % (addr,))
		exit()
	ifr = UpsInfoWidget(root, ups, desc)
	ifr.pack()
	if threading:
		root.after(10, ifr.updatethread)
	else:
		root.after(100, ifr.updatetimer)

if saveservers:
	writeservers(confpaths[0], servers)
	showinfo("upsmonitor", "Address stored in .upslist.conf")

root.mainloop()
