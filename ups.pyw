import math
from pprint import pprint
import socket
import time
import threading
import tkinter as tk
import tkinter.ttk

def hms(seconds):
	t = seconds;	h = t // 3600
	t = t % 3600;	m = t // 60
	t = t % 60;	s = t
	return "%dh %02dm" % (h, m)

def exstatus(status):
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
	return ", ".join(st)

class NutUps:
	PORT = 3493
	
	def __init__(self, hostname, upsname):
		self.hostname = hostname
		self.upsname = upsname
		self.sock = None
		self.stream = None

	def connect(self):
		print("connecting", self.hostname)
		res = socket.getaddrinfo(self.hostname,
					 self.PORT,
					 type=socket.SOCK_STREAM)
		for (af, kind, proto, cname, addr) in res:
			self.sock = socket.socket(af, kind, proto)
			self.sock.connect(addr)
			self.stream = self.sock.makefile("rw")
			break

	def tryconnect(self):
		if not self.sock:
			self.connect()

	def close(self):
		self.stream.close()
		self.sock.close()

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
			raise IOError("Protocol error: End of stream")
		elif not line:
			raise IOError("Protocol error: Empty line")
		words = self.tokenize(line)
		return words

	def recvlist(self):
		items = []
		topic = None
		while True:
			words = self.recvone()
			if words[0:2] == ["BEGIN", "LIST"]:
				if topic is not None:
					raise IOError("Protocol error: Unexpected BEGIN in the middle of a list: %r" % (words,))
				topic = words[2]
			elif words[0:2] == ["END", "LIST"]:
				if topic is None:
					raise IOError("Protocol error: Unexpected END without BEGIN: %r" % (words,))
				break
			elif words[0] == topic:
				items.append(tuple(words[1:]))
			else:
				raise IOError("Protocol error: Unexpected line: %r" % (words,))
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
			head = ("VAR", self.upsname, name)
			resp = self.recvone()
			if tuple(resp[:2]) == ("ERR", "VAR-NOT-SUPPORTED"):
				values.append(None)
			elif tuple(resp[:len(head)]) != head:
				raise IOError("Protocol error: Unexpected response: %r; expected: %r" % (resp, head))
			else:
				values.append(resp[3])
		return tuple(values)

host = "dust"
upsname = "apc"

servers = [
	("dust", "apc"),
	("ember", "rack"),
	("wind", "rack"),
]

root = tk.Tk()

root.style = tk.ttk.Style()
root.style.theme_use("classic")
#root.style.theme_use("clam")
#root.style.theme_use("default")

from itertools import count

class UpsInfoWidget:
	def _addrow(self, label, central, right=None):
		row = next(self._row)
		label = tk.ttk.Label(self._frame, text=label)
		if right:
			label.grid(row=row, column=0, sticky=tk.E)
			central.grid(row=row, column=1, sticky=tk.W)
			right.grid(row=row, column=2, sticky=tk.W)
		else:
			label.grid(row=row, column=0, sticky=tk.E)
			central.grid(row=row, column=1, columnspan=2, sticky=tk.W)
	
	def __init__(self, parent):
		self.timer = None
		
		parent = tk.ttk.Frame(parent, padding=5)
		parent.pack()
		
		frame = tk.ttk.Frame(parent, padding=(10,5,10,5))
		frame["relief"] = "groove"
		frame["borderwidth"] = 2
		frame.pack()
		
		self._frame = frame
		self._row = count()
		
		self.server_str = tk.ttk.Label(frame, justify=tk.LEFT)
		self._addrow("UPS:", self.server_str)
		
		self.status_str = tk.ttk.Label(frame, justify=tk.LEFT)
		self._addrow("Status:", self.status_str)
		
		self.batt_bar = tk.ttk.Progressbar(frame)
		self.batt_str = tk.ttk.Label(frame)
		self._addrow("Battery:", self.batt_bar, self.batt_str)
		
		self.runeta_str = tk.ttk.Label(frame)
		self._addrow("Runtime:", self.runeta_str)
		
		self.load_bar = tk.ttk.Progressbar(frame)
		self.load_str = tk.ttk.Label(frame)
		self._addrow("Load:", self.load_bar, self.load_str)
		
		self.power_str = tk.ttk.Label(frame)
		self._addrow("Power:", self.power_str)

	def updateonce(self, ups):
		self.server_str["text"] = "%s on %s" % (ups.upsname, ups.hostname)
		
		vars = ups.listvars()
		
		self.status_str["text"] = exstatus(vars["ups.status"])
		
		batt = float(vars["battery.charge"])
		load = float(vars["ups.load"])
		runeta = float(vars["battery.runtime"])
		
		self.batt_bar["value"] = int(batt)
		self.batt_str["text"] = "%.0f%%" % batt
		
		self.load_bar["value"] = int(load)
		self.load_str["text"] = "%.0f%%" % load
		
		self.runeta_str["text"] = hms(int(runeta))
		
		# VA is apparent power, W is real power (identical in DC)
		# V * A => W
		# rms(V) * rms(A) => VA
		# VA * pf => W
		
		if "ups.realpower.nominal" in vars:
			# apcupsd reports this only (no output current and only inverter voltage)
			maxrealpower = float(vars["ups.realpower.nominal"])
			realpower = maxrealpower * load / 100
			self.power_str["text"] = "approx. ~%.0fW" % realpower
		elif "ups.power.nominal" in vars:
			# Orvaldi reports these
			# power is in VA, power*powerfactor is in watts
			# output.voltage * output.current == power.nominal * load * powerfactor
			maxapprpower = float(vars["ups.power.nominal"])
			pwrfactor = float(vars["output.powerfactor"])
			realpower = maxapprpower * load / 100 * pwrfactor
		elif "output.current" in vars and "output.voltage" in vars:
			# rank this down because it'd be wrong for APC, although it would 
			# work fine for Orvaldi
			outcurrent = float(vars["output.current"])
			outvoltage = float(vars["output.voltage"])
			realpower = outcurrent * outvoltage
		
		realpower = round(realpower/10)*10
		self.power_str["text"] = "approx. ~%.0fW" % realpower
			
	def updatetimer(self, ups):
		self.updateonce(ups)
		self.timer = root.after(interval, self.updatetimer, ups)
	
	def bgupdate(self, ups):
		self.thread = threading.Thread(target=self.updateonce, args=(ups,))
		self.thread.start()
		self.timer = root.after(interval, self.bgupdate, ups)

interval = 5*1000
for h, u in servers:
	ups = NutUps(h, u)
	ifr = UpsInfoWidget(root)
	#root.after(100, ifr.bgupdate, ups)
	root.after(100, ifr.updatetimer, ups)

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
