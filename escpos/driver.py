# -*- encoding: utf-8 -*-
##############################################################################
#
#    hw_escpos Module for Odoo
#    Copyright (C) Odoo SA.
#
#    This program is free software: you can redistribute it and/or modify
#    it under the terms of the GNU Affero General Public License as
#    published by the Free Software Foundation, either version 3 of the
#    License, or (at your option) any later version.
#
#    This program is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU Affero General Public License for more details.
#
#    You should have received a copy of the GNU Affero General Public License
#    along with this program.  If not, see <http://www.gnu.org/licenses/>.
#
##############################################################################
import commands
import logging
import simplejson
import os
import os.path
import io
import base64
import time
import random
import math
import md5
import pickle
import re
import subprocess
import traceback
import usb.core
import gettext
from threading import Thread, Lock
from Queue import Queue, Empty
from PIL import Image
from pif import get_public_ip

# SLG : TODO change the incorrect import
import settings
language = gettext.translation (
    'messages',
    './translations/',
    [settings.BABEL_DEFAULT_LOCALE])
language.install(unicode = True)


from . import printer
from . import supported_devices

_logger = logging.getLogger(__name__)

class EscposDriver(Thread):
    def __init__(self):
        Thread.__init__(self)
        self.queue = Queue()
        self.lock  = Lock()
        self.vendor_product = None
        self.status = {'status':'connecting', 'messages':[]}

    def supported_devices(self):
        if not os.path.isfile('escpos_devices.pickle'):
            return supported_devices.device_list
        else:
            try:
                f = open('escpos_devices.pickle','r')
                return pickle.load(f)
                f.close()
            except Exception as e:
                self.set_status('error',str(e))
                return supported_devices.device_list

    def add_supported_device(self,device_string):
        r = re.compile('[0-9A-Fa-f]{4}:[0-9A-Fa-f]{4}');
        match = r.search(device_string)
        if match:
            match = match.group().split(':')
            vendor = int(match[0],16)
            product = int(match[1],16)
            name = device_string.split('ID')
            if len(name) >= 2:
                name = name[1]
            else:
                name = name[0]
            _logger.info('ESC/POS: adding support for device: '+match[0]+':'+match[1]+' '+name)
            
            device_list = supported_devices.device_list[:]
            if os.path.isfile('escpos_devices.pickle'):
                try:
                    f = open('escpos_devices.pickle','r')
                    device_list = pickle.load(f)
                    f.close()
                except Exception as e:
                    self.set_status('error',str(e))
            device_list.append({
                'vendor': vendor,
                'product': product,
                'name': name,
            })

            try:
                f = open('escpos_devices.pickle','w+')
                f.seek(0)
                pickle.dump(device_list,f)
                f.close()
            except Exception as e:
                self.set_status('error',str(e))

    def connected_usb_devices(self):
        connected = []
        
        for device in self.supported_devices():
            if usb.core.find(idVendor=device['vendor'], idProduct=device['product']) != None:
                connected.append(device)
        return connected

    def lockedstart(self):
        with self.lock:
            if not self.isAlive():
                self.daemon = True
                self.start()
    
    def get_escpos_printer(self):
        print "driver::get_escpos_printer"
        try:
            printers = self.connected_usb_devices()
            if len(printers) > 0:
                self.set_status(
                    'connected',
                    _(u'Connected to %s') %(printers[0]['name']))
                self.vendor_product = str(printers[0]['vendor']) + '_' + str(printers[0]['product'])
                return printer.Usb(printers[0]['vendor'], printers[0]['product'])
            else:
                self.set_status(
                    'disconnected',
                    _(u'Printer Not Found'))
                self.vendor_product = None
                return None
        except Exception as e:
            self.set_status('error',str(e))
            self.vendor_product = None
            return None

    def get_status(self):
        self.push_task('status')
        return self.status

    def get_vendor_product(self):
        self.push_task('status')
        return self.vendor_product

    def open_cashbox(self,printer):
        printer.cashdraw(2)
        printer.cashdraw(5)

    def set_status(self, status, message = None):
        _logger.info(status+' : '+ (message or 'no message'))
        if status == self.status['status']:
            if message != None and (len(self.status['messages']) == 0 or message != self.status['messages'][-1]):
                self.status['messages'].append(message)
        else:
            self.status['status'] = status
            if message:
                self.status['messages'] = [message]
            else:
                self.status['messages'] = []

        if status == 'error' and message:
            _logger.error('ESC/POS Error: '+message)
        elif status == 'disconnected' and message:
            _logger.warning('ESC/POS Device Disconnected: '+message)

    def run(self):
        while True:
            try:
                timestamp, task, data = self.queue.get(True)

                printer = self.get_escpos_printer()

                if printer == None:
                    if task != 'status':
                        self.queue.put((timestamp,task,data))
                    time.sleep(5)
                    continue
                elif task == 'receipt': 
                    if timestamp >= time.time() - 1 * 60 * 60:
                        self.print_receipt_body(printer,data)
                        printer.cut()
                elif task == 'xml_receipt':
                    if timestamp >= time.time() - 1 * 60 * 60:
                        printer.receipt(data)
                elif task == 'cashbox':
                    if timestamp >= time.time() - 12:
                        self.open_cashbox(printer)
                elif task == 'printstatus':
                    self.print_status(printer)
                elif task == 'status':
                    pass

            except Exception as e:
                self.set_status('error', str(e))
                errmsg = str(e) + '\n' + '-'*60+'\n' + traceback.format_exc() + '-'*60 + '\n'
                _logger.error(errmsg);

    def push_task(self,task, data = None):
        self.lockedstart()
        self.queue.put((time.time(),task,data))

    def print_status(self,eprint):
        ip = get_public_ip()
        eprint.text('\n\n')
        eprint.set(align='center',type='b',height=2,width=2)
        eprint.text(_(u'PosBox Status'))
        eprint.text('\n\n')
        eprint.set(align='center')

        if not ip:
            msg = _(
                """ERROR: Could not connect to LAN\n\n"""
                """Please check that the PosBox is correc-\n"""
                """tly connected with a network cable,\n"""
                """ that the LAN is setup with DHCP, and\n"""
                """that network addresses are available""")
            eprint.text(msg)
        else:
            eprint.text(_(u'IP Address:') + '\n'+ ip +'\n')
            eprint.text('\n' + _(u'Homepage:') + '\n')
            eprint.text('http://'+ip+':' + str(settings.FLASK_PORT) + '\n')

        eprint.text('\n\n')
        eprint.cut()

    def print_receipt_body(self,eprint,receipt):

        def check(string):
            return string != True and bool(string) and string.strip()
        
        def price(amount):
            return ("{0:."+str(receipt['precision']['price'])+"f}").format(amount)
        
        def money(amount):
            return ("{0:."+str(receipt['precision']['money'])+"f}").format(amount)

        def quantity(amount):
            if math.floor(amount) != amount:
                return ("{0:."+str(receipt['precision']['quantity'])+"f}").format(amount)
            else:
                return str(amount)

        def printline(left, right='', width=40, ratio=0.5, indent=0):
            lwidth = int(width * ratio) 
            rwidth = width - lwidth 
            lwidth = lwidth - indent
            
            left = left[:lwidth]
            if len(left) != lwidth:
                left = left + ' ' * (lwidth - len(left))

            right = right[-rwidth:]
            if len(right) != rwidth:
                right = ' ' * (rwidth - len(right)) + right

            return ' ' * indent + left + right + '\n'
        
        def print_taxes():
            taxes = receipt['tax_details']
            for tax in taxes:
                eprint.text(printline(tax['tax']['name'],price(tax['amount']), width=40,ratio=0.6))

        # Receipt Header
        if receipt['company']['logo']:
            eprint.set(align='center')
            eprint.print_base64_image(receipt['company']['logo'])
            eprint.text('\n')
        else:
            eprint.set(align='center',type='b',height=2,width=2)
            eprint.text(receipt['company']['name'] + '\n')

        eprint.set(align='center',type='b')
        if check(receipt['company']['contact_address']):
            eprint.text(receipt['company']['contact_address'] + '\n')
        if check(receipt['company']['phone']):
            eprint.text(_(u'Tel: ') + receipt['company']['phone'] + '\n')
        if check(receipt['company']['vat']):
            eprint.text(_(u'VAT: ') + receipt['company']['vat'] + '\n')
        if check(receipt['company']['email']):
            eprint.text(receipt['company']['email'] + '\n')
        if check(receipt['company']['website']):
            eprint.text(receipt['company']['website'] + '\n')
        if check(receipt['header']):
            eprint.text(receipt['header']+'\n')
        if check(receipt['cashier']):
            eprint.text('-'*32+'\n')
            eprint.text(_(u'Served by ') + receipt['cashier']+'\n')

        # Orderlines
        eprint.text('\n\n')
        eprint.set(align='center')
        for line in receipt['orderlines']:
            pricestr = price(line['price_display'])
            if line['discount'] == 0 and line['unit_name'] == 'Unit(s)' and line['quantity'] == 1:
                eprint.text(printline(line['product_name'],pricestr,ratio=0.6))
            else:
                eprint.text(printline(line['product_name'],ratio=0.6))
                if line['discount'] != 0:
                    eprint.text(printline('Discount: '+str(line['discount'])+'%', ratio=0.6, indent=2))
                if line['unit_name'] == 'Unit(s)':
                    eprint.text( printline( quantity(line['quantity']) + ' x ' + price(line['price']), pricestr, ratio=0.6, indent=2))
                else:
                    eprint.text( printline( quantity(line['quantity']) + line['unit_name'] + ' x ' + price(line['price']), pricestr, ratio=0.6, indent=2))

        # Subtotal if the taxes are not included
        taxincluded = True
        if money(receipt['subtotal']) != money(receipt['total_with_tax']):
            eprint.text(printline('','-------'));
            eprint.text(printline(_(u'Subtotal'),money(receipt['subtotal']),width=40, ratio=0.6))
            print_taxes()
            #eprint.text(printline(_(u'Taxes'),money(receipt['total_tax']),width=40, ratio=0.6))
            taxincluded = False


        # Total
        eprint.text(printline('','-------'));
        eprint.set(align='center',height=2)
        eprint.text(printline(
            _(u'         TOTAL'),
            money(receipt['total_with_tax']), width=40, ratio=0.6))
        eprint.text('\n\n');
        
        # Paymentlines
        eprint.set(align='center')
        for line in receipt['paymentlines']:
            eprint.text(printline(
                line['journal'], money(line['amount']), ratio=0.6))

        eprint.text('\n');
        eprint.set(align='center',height=2)
        eprint.text(printline(
            _(u'        CHANGE'),
            money(receipt['change']),width=40, ratio=0.6))
        eprint.set(align='center')
        eprint.text('\n');

        # Extra Payment info
        if receipt['total_discount'] != 0:
            eprint.text(printline(
                _(u'Discounts'),
                money(receipt['total_discount']),width=40, ratio=0.6))
        if taxincluded:
            print_taxes()
            #eprint.text(printline(_(u'Taxes'),money(receipt['total_tax']),width=40, ratio=0.6))

        # Footer
        if check(receipt['footer']):
            eprint.text('\n'+receipt['footer']+'\n\n')
        eprint.text(receipt['name']+'\n')
        eprint.text(      str(receipt['date']['date']).zfill(2)
                    +'/'+ str(receipt['date']['month']+1).zfill(2)
                    +'/'+ str(receipt['date']['year']).zfill(4)
                    +' '+ str(receipt['date']['hour']).zfill(2)
                    +':'+ str(receipt['date']['minute']).zfill(2) )





