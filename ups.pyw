# -*- coding: utf-8; indent-tabs-mode: t; tab-width: 4 -*-
from __future__ import print_function
from itertools import count
import math
import os
from pprint import pprint
import shlex
import socket
import struct
import sys
import time
import threading

if sys.version_info.major == 2:
	import Tkinter as tk
	import tkFont as tkfont
	ttk = None
else:
	import tkinter as tk
	import tkinter.font as tkfont
	import tkinter.ttk as ttk
	#ttk = None

def loadservers(path):
	# .ups.conf contains a list of UPS addreses, one 'ups@host' per line, with
	# optional description after the address. An address with only '@host' means
	# an apcupsd server instead of a NUT server.

	# Note: 'with' is only in 2.6 and later
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

def clamp(x, low, high):
	return min(max(x, low), high)

def hms(seconds):
	t = seconds;	h = t // 3600
	t = t % 3600;	m = t // 60
	t = t % 60;	s = t
	return "%dh %02dm" % (h, m)

def nutstrstatus(status):
	long = {
		"OL": "on line power",
		"OB": "on battery",
		"LB": "battery low",
		"HB": "battery high",
		"RB": "replace battery",
		"BYPASS": "bypass",
		"CAL": "calibrating",
		"OFF": "output offline",
		"OVER": "overload",
		"TRIM": "trimming",
		"BOOST": "boosting",
		"FSD": "forced shutdown",
	}
	st = []
	for w in status.split():
		st.append(long.get(w, w))
	return ", ".join(st) #.capitalize()

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

def softclose(file):
	if file is not None:
		try:
			file.close()
		except:
			pass

class NutError(Exception):
	pass

class NutProtocolError(IOError):
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
		self.stream = softclose(self.stream)
		self.sock = softclose(self.sock)

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
			raise NutProtocolError("End of stream")
		elif not line:
			raise NutProtocolError("Empty line")
		words = self.tokenize(line)
		return words

	def recvlist(self):
		items = []
		topic = None
		while True:
			resp = self.recvone()
			if resp[0] == "ERR":
				raise NutError(*resp[1:])
			elif resp[0] == "BEGIN":
				if topic is not None:
					raise NutProtocolError("BEGIN in the middle of a list: %r" % (resp,))
				if len(resp) < 3 or resp[1] != "LIST":
					raise NutProtocolError("Not enough parameters: %r" % (resp,))
				topic = resp[2]
			elif resp[0] == "END":
				if topic is None:
					raise NutProtocolError("END without BEGIN: %r" % (resp,))
				if len(resp) < 3 or resp[1] != "LIST":
					raise NutProtocolError("Not enough parameters: %r" % (resp,))
				break
			elif resp[0] == topic:
				items.append(tuple(resp[1:]))
			else:
				raise NutProtocolError("Unexpected: %r" % (resp,))
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
				raise NutError(*resp[1:])
		elif resp[0] == "VAR":
			if len(resp) < 4:
				raise NutProtocolError("Not enough parameters: %r" % (resp,))
			elif resp[1:3] != (self.upsname, name):
				raise NutProtocolError("Desynchronized: %r; expected: %r" % (resp, name))
			return resp[3]
		else:
			raise NutProtocolError("Unexpected: %r" % (resp,))

class ApcupsdUps(Ups):
	PORT = 3551
	FMODE = "rwb"
	
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
				raise NutProtocolError("Status did not start with 'APC' key: %r" % [key, val])
			if "END APC" in vars:
				raise NutProtocolError("Unexpected variable after 'END APC': %r" % [key, val])
			vars[key] = val
		if "END APC" not in vars:
			raise NutProtocolError("Status did not finish with 'END APC' key: %r" % vars)
		return vars
	
	def listvars(self):
		intmap = {
			"BATTV":	"battery.voltage",
			"BCHARGE": 	"battery.charge",
			"LINEV":	"input.voltage",
			"LOADPCT":	"ups.load",
			"NOMPOWER":	"ups.realpower.nominal",
		}
		avars = self.getstatus()
		nvars = {}
		for akey, aval in avars.items():
			if akey in intmap:
				nvars[intmap[akey]] = float(aval.split()[0])
			elif akey == "TIMELEFT":
				aval, unit = aval.split()
				assert unit == "Minutes"
				nvars["battery.runtime"] = float(aval) * 60
			elif akey == "STATUS":
				aval = aval.split()
				nval = []
				for v in aval:
					if v == "ONLINE":
						nval.append("OL")
				nvars["ups.status"] = " ".join(nval) or "UNKNOWN"
		return nvars
			
if ttk:
	TkFrame = ...
	TkLabel = ttk.Label
	TkLabelFrame = ...
	TkProgressBar = ttk.Progressbar

	def cnfpadding(cnf):
		padx = 0
		if "padx" in cnf:
			padx = cnf["padx"]
			del cnf["padx"]
		pady = 0
		if "pady" in cnf:
			pady = cnf["pady"]
			del cnf["pady"]
		if padx or pady:
			# left, top, right, bottom
			cnf["padding"] = (padx, pady, padx, pady)
		return cnf

	class TkFrame(ttk.Frame):
		def __init__(self, master=None, cnf={}, **kw):
			cnf = tk._cnfmerge((cnf, kw))
			cnf = cnfpadding(cnf)
			ttk.Frame.__init__(self, master, **cnf)

	class TkLabelFrame(ttk.LabelFrame):
		def __init__(self, master=None, cnf={}, **kw):
			cnf = tk._cnfmerge((cnf, kw))
			cnf = cnfpadding(cnf)
			ttk.LabelFrame.__init__(self, master, **cnf)

else:
	TkFrame = tk.Frame
	TkLabel = tk.Label
	TkLabelFrame = ...
	TkProgressBar = ...

	class TkCustomWidget:
		def config(self, **kv):
			for key, value in kv.items():
				self[key] = value

		configure = config

		def pack(self, *a, **kw):
			self.outer.pack(*a, **kw)

		def grid(self, *a, **kw):
			self.outer.grid(*a, **kw)

	class TkLabelFrame(tk.LabelFrame):
		def __init__(self, master=None, cnf={}, **kw):
			tk.LabelFrame.__init__(self, master, cnf, **kw)
			#self["font"] = "%s bold" % self["font"]
			# default is "{MS Sans Serif} 8" on Win98, but "TkDefaultFont" on Win11

	class TkProgressBar(TkCustomWidget):
		def __init__(self, master=None, *, value=0, length=100, height=12):
			self.value = value
			self.width = length
			self.height = height
			self.outer = tk.Frame(master, borderwidth=2, relief="sunken", padx=1, pady=1)
			self.bg = tk.Frame(self.outer)#, bg="#999999")
			self.bg.columnconfigure(0, minsize=self.width)
			self.bg.rowconfigure(0, minsize=self.height)
			self.bg.pack()
			self.bar = tk.Frame(self.bg, width=0, height=self.height, bg="#4a6984")
			# bg="#996666"
			self.bar.grid_propagate(0)
			self.bar.grid(row=0, column=0, sticky=tk.N+tk.S+tk.W)

		def __setitem__(self, key, value):
			if key == "value":
				self.value = clamp(value, 0, 100)
				self.bar.config(width=int(self.width / 100.0 * self.value))
			else:
				raise KeyError(key)

class UpsInfoWidget:
	def _addrow(self, label, central, right=None):
		row = next(self._row)
		label = TkLabel(self._frame, text=label)
		if right:
			label.grid(row=row, column=0, sticky=tk.E, padx=2)
			central.grid(row=row, column=1, sticky=tk.W)
			right.grid(row=row, column=2, sticky=tk.W, padx=2)
		else:
			label.grid(row=row, column=0, sticky=tk.E, padx=2)
			central.grid(row=row, column=1, columnspan=2, sticky=tk.W)

	def __init__(self, parent, ups, title):
		if not title:
			title = "%s on %s" % (ups.upsname, ups.hostname)

		self.ups = ups
		self.title = title
		self.timer = None
		self.valid = True

		# Outer padding (between elements)
		parent = TkFrame(parent, padx=5, pady=5)
		parent.pack()

		frame = TkLabelFrame(parent, padx=5, pady=3)
		frame.pack()

		self._frame = frame
		self._row = count()

		#self.server_str = TkLabel(frame, justify=tk.LEFT)
		#self._addrow("UPS:", self.server_str)

		self.status_str = TkLabel(frame, justify=tk.LEFT)
		self._addrow("Status:", self.status_str)

		self.batt_bar = TkProgressBar(frame, length=120)
		self.batt_str = TkLabel(frame)
		self._addrow("Battery:", self.batt_bar, self.batt_str)

		self.runeta_str = TkLabel(frame)
		self._addrow("Runtime:", self.runeta_str)

		self.load_bar = TkProgressBar(frame, length=120)
		self.load_str = TkLabel(frame)
		self._addrow("Load:", self.load_bar, self.load_str)

		self.power_str = TkLabel(frame)
		self._addrow("Power:", self.power_str)

		self._frame.config(text=self.title)
		#self.server_str.config(text=self.title)
		self.updateclear(text="connecting")

	def softlistvars(self, isretry=False):
		if not self.valid:
			return None

		try:
			return self.ups.listvars()
		except (OSError, IOError) as e:
			print("error (%r): %r" % (self.ups, e))
			self.ups.close()
			if isretry:
				return None
			self.updateclear("connection lost")
			return self.softlistvars(isretry=True)
		except NutError as e:
			print("error (%r): %r" % (self.ups, e))
			self.ups.close()
			self.valid = False
			self.updateclear("invalid: %s" % e.args[0])
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
		if not self.valid:
			self.status_str.config(state=tk.DISABLED)

	def updateonce(self):
		vars = self.softlistvars()
		if not vars:
			return

		batt = float(vars["battery.charge"])
		load = float(vars["ups.load"])
		runeta = float(vars["battery.runtime"])
		realpower = nutgetpower(vars)
		runeta = round(runeta / 600) * 600		# 10 min. precision
		realpower = round(realpower / 10) * 10	# 10 W precision

		self.status_str.config(state=tk.NORMAL, text=nutstrstatus(vars["ups.status"]))
		self.batt_bar.config(value=int(batt))
		self.batt_str.config(state=tk.NORMAL, text="%.0f%%" % batt)
		self.load_bar.config(value=int(load))
		self.load_str.config(state=tk.NORMAL, text="%.0f%%" % load)
		self.runeta_str.config(state=tk.NORMAL, text="approx. %s" % hms(int(runeta)))
		self.power_str.config(state=tk.NORMAL, text="approx. %dW" % realpower)

	def updatetimer(self):
		self.updateonce()
		self.timer = root.after(interval, self.updatetimer)

	def bgupdate(self):
		self.thread = threading.Thread(target=self.updateonce)
		self.thread.start()
		self.timer = root.after(interval, self.bgupdate)
		
root = tk.Tk()

if ttk:
	# It seems that Ttk has magic for determining the correct family and
	# size of 'TkDefaultFont', such that any change (e.g. weight=BOLD)
	# will break it and no size value is right. Fortunately, we kind of
	# want to make it larger and more prominent anyway.
	#deffont = tkfont.nametofont("TkDefaultFont")
	#boldfont = deffont.copy()
	#boldfont.configure(weight=tkfont.BOLD)
	ttk.Style().configure("TLabelframe.Label", font=("TkDefaultFont", -12, tkfont.BOLD))

confpath = [
	os.path.join(sys.path[0], ".ups.conf"),
	os.path.expanduser("~/.ups.conf"),
]
servers = loadservers(confpath[0])
interval = 5*1000
ttkstyle = None
#ttkstyle = "classic"
#ttkstyle = "clam"
#ttkstyle = "default"

# sys.platform	os.name
# "win32"		"nt"

if sys.platform == "win32":
	if sys.getwindowsversion()[:3] == (5, 1, 2600):
		#ttkstyle = "classic"
		pass

if ttk and ttkstyle:
	ttk.Style().theme_use(ttkstyle)

root.title("UPS status")

for addr, desc in servers:
	if addr.startswith("@"):
		ups = ApcupsdUps("apcupsd" + addr)
	else:
		ups = NutUps(addr)
	ifr = UpsInfoWidget(root, ups, desc)
	root.after(100, ifr.bgupdate)
	#root.after(100, ifr.updatetimer)

#def server_changed(new_value):
#	if infoframe.timer:
#		root.after_cancel(infoframe.timer)
#		infoframe.timer = None
#server_var = tk.StringVar(frame, value="Select server")
#server_strs = [pair2str(h, u) for (h, u) in servers]
#server_menu = tk.OptionMenu(frame, server_var, *server_strs, command=server_changed)
#server_menu.pack()

root.mainloop()
