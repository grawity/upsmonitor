# -*- coding: utf-8 -*-
from __future__ import print_function
from itertools import count
import math
from pprint import pprint
import socket
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

servers = [
	("ember", "rack", None),
	("wind", "rack", None),
	("dust", "apc", None),
	#("ember", "vol5", "orvaldi on lnx1"),
]
interval = 5*1000
ttkstyle = None
#ttkstyle = "classic"
#ttkstyle = "clam"
#ttkstyle = "default"

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
	# VA is apparent power, W is real power (identical in DC)
	# V * A => W
	# rms(V) * rms(A) => VA
	# VA * pf => W
	if "ups.realpower.nominal" in vars:
		# apcupsd reports this only (no output current and only inverter voltage)
		maxrealpower = float(vars["ups.realpower.nominal"])
		curload = float(vars["ups.load"])
		realpower = maxrealpower * curload / 100
	elif "ups.power.nominal" in vars:
		# Orvaldi reports these
		# power is in VA, power*powerfactor is in watts
		# output.voltage * output.current == power.nominal * load * powerfactor
		maxapprpower = float(vars["ups.power.nominal"])
		curload = float(vars["ups.load"])
		pwrfactor = float(vars["output.powerfactor"])
		realpower = maxapprpower * curload / 100 * pwrfactor
	elif "output.current" in vars and "output.voltage" in vars:
		# rank this down because it'd be wrong for APC, although it would
		# work fine for Orvaldi
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

class NutUps:
	PORT = 3493

	def __init__(self, hostname, upsname):
		self.hostname = hostname
		self.upsname = upsname
		self.sock = None
		self.stream = None
	
	def __repr__(self):
		return "NutUps(%s@%s)" % (self.upsname, self.hostname)

	def connect(self):
		print("connecting", self.hostname)
		res = socket.getaddrinfo(self.hostname,
					 self.PORT,
					 socket.AF_UNSPEC,
					 socket.SOCK_STREAM)
		for (af, kind, proto, cname, addr) in res:
			self.sock = socket.socket(af, kind, proto)
			self.sock.connect(addr)
			self.stream = self.sock.makefile("rw")
			break

	def tryconnect(self):
		if not self.sock:
			self.connect()

	def close(self):
		softclose(self.stream)
		self.stream = None
		softclose(self.sock)
		self.sock = None

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
		import shlex
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
				topic = resp[2]
			elif resp[0] == "END":
				if topic is None:
					raise NutProtocolError("END without BEGIN: %r" % (resp,))
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
		(val,) = self.getvars(name)
		if val is None:
			raise KeyError(val)
		return val

	def getvars(self, *names):
		self.tryconnect()
		values = []
		for name in names:
			head = ("VAR", self.upsname, name)
			self.send("GET %s %s %s" % head)
		for name in names:
			head = ["VAR", self.upsname, name]
			resp = self.recvone()
			if resp[0] == "ERR":
				if resp[1] == "VAR-NOT-SUPPORTED":
					values.append(None)
				else:
					raise NutError(*resp[1:])
			elif resp[0] == "VAR":
				print(resp[1:3])
				if len(resp) < 4:
					raise NutProtocolError("Not enough parameters: %r" % (resp,))
				elif resp[1:3] != head[1:3]:
					raise NutProtocolError("Desynchronized: %r; expected: %r" % (resp, head))
				values.append(resp[3])
			else:
				raise NutProtocolError("Unexpected: %r; expected: %r" % (resp, head))
		return tuple(values)


root = tk.Tk()

if ttk:
	style = ttk.Style()
	if ttkstyle:
		style.theme_use(ttkstyle)
	# It seems that Ttk has magic for determining the correct family and
	# size of 'TkDefaultFont', such that any change (e.g. weight=BOLD)
	# will break it and no size value is right. Fortunately, we kind of
	# want to make it larger and more prominent anyway.
	#deffont = tkfont.nametofont("TkDefaultFont")
	#boldfont = deffont.copy()
	#boldfont.configure(weight=tkfont.BOLD)
	style.configure("TLabelframe.Label", font=("TkDefaultFont", -12, tkfont.BOLD))

	tk.Label = ttk.Label
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
	def asciibar(value, width=20, fill="#", space="."):
		value = int(width / 100.0 * value)
		return "%s" % "".ljust(value, fill).ljust(width, space)

	TkFrame = tk.Frame
	#TkLabelFrame = tk.LabelFrame
	
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
			# default is "{MS Sans Serif} 8" on Win98

	class TkRawAsciiProgressBar(tk.Label):
		def __init__(self, master=None, cnf={}, **kw):
			cnf = tk._cnfmerge((cnf, kw))
			cnf["font"] = ("Terminal", 9)
			tk.Label.__init__(self, master, cnf)

		def configure(self, cnf):
			if "value" in cnf:
				cnf["text"] = asciibar(cnf["value"])
				del cnf["value"]
			tk.Label.configure(self, cnf)

	class TkAsciiProgressBar(TkCustomWidget):
		def __init__(self, master=None, length=100):
			# flat, solid, groove, ridge, raised, l
			self.width = length // 6 # approximately 6 px per char
			self.outer = tk.Frame(master, borderwidth=2, relief="groove")
			self.inner = tk.Label(self.outer, font=("Terminal", 9))
			self.inner.pack()
			self.config(value=0)

		def __setitem__(self, key, value):
			if key == "value":
				self.inner["text"] = asciibar(value, width=self.width, space=" ")
			else:
				raise KeyError(key)

	class TkProgressBar(TkCustomWidget):
		def __init__(self, master=None, length=100, height=12):
			self.width = length
			self.height = height
			self.outer = tk.Frame(master, borderwidth=2, relief="groove", padx=1, pady=1)
			self.bg = tk.Frame(self.outer)#, bg="#999999")
			self.bg.columnconfigure(0, minsize=self.width)
			self.bg.rowconfigure(0, minsize=self.height)
			self.bg.pack()
			self.bar = tk.Frame(self.bg, width=0, height=self.height, bg="#996666")
			self.bar.grid_propagate(0)
			self.bar.grid(row=0, column=0, sticky=tk.N+tk.S+tk.W)

		def __setitem__(self, key, value):
			if key == "value":
				self.bar["width"] = int(self.width / 100.0 * value)
			else:
				raise KeyError(key)

	#TkProgressBar = TkAsciiProgressBar

class UpsInfoWidget:
	def _addrow(self, label, central, right=None):
		row = next(self._row)
		label = tk.Label(self._frame, text=label)
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

		parent = TkFrame(parent, padx=5, pady=5)
		parent.pack()

		#frame = TkFrame(parent, padx=10, pady=5, borderwidth=2, relief="groove")
		frame = TkLabelFrame(parent, padx=10, pady=5)
		frame.pack()

		self._frame = frame
		self._row = count()

		#self.server_str = tk.Label(frame, justify=tk.LEFT)
		#self._addrow("UPS:", self.server_str)

		self.status_str = tk.Label(frame, justify=tk.LEFT)
		self._addrow("Status:", self.status_str)

		self.batt_bar = TkProgressBar(frame, length=120)
		self.batt_str = tk.Label(frame)
		self._addrow("Battery:", self.batt_bar, self.batt_str)

		self.runeta_str = tk.Label(frame)
		self._addrow("Runtime:", self.runeta_str)

		self.load_bar = TkProgressBar(frame, length=120)
		self.load_str = tk.Label(frame)
		self._addrow("Load:", self.load_bar, self.load_str)

		self.power_str = tk.Label(frame)
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
			if self.isretry:
				return None
			else:
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
		runeta = round(runeta / 600) * 600	# 10 min. precision
		realpower = round(realpower, -1)	# 10 W precision
		
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

root.title("UPS status")
for host, name, desc in servers:
	ups = NutUps(host, name)
	ifr = UpsInfoWidget(root, ups, desc)
	#root.after(100, ifr.bgupdate)
	root.after(100, ifr.updatetimer)

#def pair2str(hostname, upsname):
#	return "%s on %s" % (upsname, hostname)
#def str2pair(string):
#	upsname, hostname = string.split(" on ")
#	return hostname, upsname
#def server_changed(new_value):
#	global ups
#	global infoframe
#	hostname, upsname = str2pair(new_value)
#	if infoframe.timer:
#		root.after_cancel(infoframe.timer)
#		infoframe.timer = None
#	if ups:
#		ups.close()
#	ups = NutUps(hostname, upsname)
#	infoframe.update(ups)
#ups = None
#frame = tk.ttk.Frame(root, padding=(5,5,5,0))
#frame.pack()
#server_var = tk.StringVar(frame, value="Select server")
#server_strs = [pair2str(h, u) for (h, u) in servers]
#server_menu = tk.OptionMenu(frame, server_var, *server_strs, command=server_changed)
#server_menu.pack()
#infoframe = UpsInfoWidget(root)

root.mainloop()
