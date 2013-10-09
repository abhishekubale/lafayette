#!/usr/bin/env python
# encoding: utf-8

#Copyright 2012 Linkedin
#
#Licensed under the Apache License, Version 2.0 (the "License");
#you may not use this file except in compliance with the License.
#You may obtain a copy of the License at
#
#http://www.apache.org/licenses/LICENSE-2.0
#
#Unless required by applicable law or agreed to in writing, software
#distributed under the License is distributed on an "AS IS" BASIS,
#WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#See the License for the specific language governing permissions and
#limitations under the License.
#

from flask import Flask, request, session, g, redirect, url_for, \
     abort, render_template, flash, Response, request
import MySQLdb
import dns.resolver
import os
from pprint import pprint
from datetime import date, datetime, timedelta
import dns.resolver
import re
import socket,struct
import unicodedata
import signal
import urlparse
import tempfile
import subprocess

from ConfigParser import SafeConfigParser

import smtplib
import email
from email import encoders
from email.message import Message
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase

from forensic_auth import is_authorized

app = Flask(__name__)

# Local config
#
config = SafeConfigParser()
filename = os.path.join(app.root_path, 'forensic.cfg')
found=config.readfp(open(filename))

app.secret_key = config.get('web','secret_key')
reportSender = config.get('web','reportSender')
mailSmtp = config.get('web','mailSmtp')

dbHost=config.get('db','dbHost')
dbUser=config.get('db','dbUser')
dbName=config.get('db','dbName')
dbPassword=config.get('db','dbPassword')

reportEmailCc=config.get('reports','email')
reportEmailSpamCc=config.get('reports','emailSpam')
arfPassword=config.get('reports','arfPassword')

zen=config.get('dnsbl','zen')
wldomain=config.get('dnsbl','wldomain').split(",")

# end of local config

privatenet = ["127.0.0.0/8","192.168.0.0/16","172.16.0.0/12","10.0.0.0/8"]

def handleTimeOut(signum, frame0):
   raise TimeoutError("taking too long")

def addressInNetworkList(ip,netlist):
    for net in netlist:
        if addressInNetwork(ip,net):
            return True
    return False

def addressInNetwork(ip, net):
   ipaddr = int(''.join([ '%02x' % int(x) for x in ip.split('.') ]), 16)
   netstr, bits = net.split('/')
   netaddr = int(''.join([ '%02x' % int(x) for x in netstr.split('.') ]), 16)
   mask = (0xffffffff << (32 - int(bits))) & 0xffffffff
   return (ipaddr & mask) == (netaddr & mask)

def getIp4ToAsn(ip):
    asn = 0
    try:
        (ip1,ip2,ip3,ip4) = ip.split(".")
        query = "%s.%s.%s.%s.origin.asn.cymru.com" % (ip4,ip3,ip2,ip1)
        reportanswers = dns.resolver.query(query, 'TXT')
        res = reportanswers[0].to_text()
        asn = long(res.split("|")[0][1:])
    except:
        pass
    return asn

def getAsnInfo(asn):
    resAsn = ""
    countryCode = ""
    rir = ""
    createDate =""
    name = ""
    try:
        query = "AS%s.asn.cymru.com" % (asn)
        reportanswers = dns.resolver.query(query, 'TXT')
        info = reportanswers[0].to_text()[1:-1]
        (resAsn,countryCode,rir,createDate,name) = info.split("|",4)
    except:
        pass
    return (resAsn,countryCode,rir,createDate,name)

def getEmailAbuseFromAsn(asn):
    res=""
    strSql="select email from asn where asn=%s" % str(asn)
    g.db.query(strSql)
    result = g.db.store_result()
    if result is not None:
        try:
            row = result.fetch_row(1,1)[0]
            res = row['email']
        except:
            res = ""
    return res

def getEmailAbuseFromIp(ip):
    res=""
    try:
        (ip1,ip2,ip3,ip4) = ip.split(".")
        query = "%s.%s.%s.%s.abuse-contacts.abusix.org" % (ip4,ip3,ip2,ip1)
        reportanswers = dns.resolver.query(query, 'TXT')
        res = reportanswers[0].to_text()
        res = res[1:-1]
    except:
        pass
    return res

def getZen(ip):
    global zen
    if zen is None:
        return ""
    res="N"
    try:
        (ip1,ip2,ip3,ip4) = ip.split(".")
        query = "%s.%s.%s.%s.%s" % (ip4,ip3,ip2,ip1,zen)
        reportanswers = dns.resolver.query(query, 'A')
        res = reportanswers[0].to_text()
        if res != "127.0.0.1":
            res="Z"
    except:
        res=""
        pass
    return res

def addDnsbl(entries):
    if entries is None:
        return entries
    for entry in entries:
        zen=getZen(entry["sourceIp"])
        entry.update(dnsbl=zen)
    return entries


def sendArf(item, spam=False):
    global reportSender
    global mailSmtp
    global reportEmailCc
    global reportEmailSpamCc

    msg = MIMEBase('multipart','report')
    msg.set_param('report-type','feedback-report',requote=False)

    msg["To"] = str(item['emailAbuse'])
    msg["From"] = reportSender
    msg["Subject"] = "Abuse report for: "+str(item['subject'])

    if spam:
        text = "This is an email in the abuse report format (ARF) for an email message coming via these \r\n"
        text = text+"IPs "+str(item['sourceIp'])+" on "+str(item['arrivalDate'])+".\r\n"
        text = text+"This report indicates that the attached email was not wanted by the recipient.\r\n"
        text = text+"This report may indicates a compromised machine and may contain URLs to malware, treat with caution!\r\n\r\n"
        text = text+"This ARF report contains all the information you will need to assess the problem.\r\n"
        text = text+"The zip attachment is the complete email encrypted with the password "+str(arfPassword)+"\r\n";
        text = text+"For more information about this format please see http://tools.ietf.org/html/rfc5965.\r\n";
    else:
        text = "This is an email in the abuse report format (ARF) for an email message received from \r\n"
        text = text+"IP "+str(item['sourceIp'])+" "+str(item['sourceDomain'])+" on "+str(item['arrivalDate'])+" UTC.\r\n"
        text = text+"This report likely indicates a compromised machine and may contain URLs to malware, treat with caution!\r\n\r\n"
        text = text+"The attached email was selected amongst emails that failed DMARC,\r\n"
        text = text+"therefore it indicates that the author tried to pass for someone else\r\n"
        text = text+"indicating fraud and not spam. The faster you fix or isolate the compromised machine, \r\n"
        text = text+"the better you protect your customers or members and the Internet at large.\r\n\r\n"
        text = text+"This ARF report contains all the information you will need to assess the problem.\r\n"
        text = text+"The zip attachment is the complete email encrypted with the password "+str(arfPassword)+"\r\n";
        text = text+"For more information about this format please see http://tools.ietf.org/html/rfc5965.\r\n";

    msgtxt = MIMEText(text)
    msg.attach(msgtxt)

    msgreport = MIMEBase('message', "feedback-report")
    msgreport.set_charset("US-ASCII")
    
    if spam:
        text = "Feedback-Type: abuse\r\n"
    else:
        text = "Feedback-Type: fraud\r\n"
    text = text + "User-Agent: pyforensic/1.1\r\n"
    text = text + "Version: 1.0\r\n"
    if not spam:
        text = text + "Source-IP: "+str(item['sourceIp'])+"\r\n"
    else:
        ipList = item['sourceIp'].split(", ")
        for ip in ipList:
            text = text + "Source-IP: "+str(ip)+"\r\n"

    text = text + "Arrival-Date: "+str(item['arrivalDate'])+" UTC\r\n"

    text = text + "Attachment-Password: "+str(arfPassword)+"\r\n"

    if 'urlList' in item:
        for uri in item['urlList']:
            o = urlparse.urlparse(uri)
            urlReport=True
            if o.hostname is not None:
                for domain in wldomain:
                    if o.hostname[-len(domain):]==domain:
                        urlReport=False
                if urlReport==True:
                    text = text + "Reported-Uri: "+str(uri)+"\r\n"

    msgreport.set_payload(text)
    msg.attach(msgreport)

    #msgrfc822 = MIMEBase('message', "rfc822")
    msgrfc822 = MIMEBase('text', "rfc822-headers")
    msgrfc822.add_header('Content-Disposition','inline')
    parts=re.split(r'\r\n\r\n|\n\n',item['content'])
    rfc822headers=parts[0]
    #msgrfc822.set_payload(item['content'])
    msgrfc822.set_payload(rfc822headers)
    
    msg.attach(msgrfc822)

    #prepare the zip encrypted
    temp=tempfile.NamedTemporaryFile(prefix='mail',suffix='.eml',delete=False)
    tempname=temp.name
    temp.write(item['content'])
    temp.flush()
    ziptemp = tempfile.NamedTemporaryFile(prefix='mail',suffix='.zip',delete=True)
    ziptempname=ziptemp.name
    ziptemp.close()
    workdir = os.path.dirname(ziptempname)
    filenamezip = os.path.basename(ziptempname)
    filenameemail = os.path.basename(tempname)
    os.chdir(workdir)
    option = '-P%s' % arfPassword
    rc = subprocess.call(['zip', option] + [filenamezip, filenameemail])
    temp.close()

    
    ziptemp = open(ziptempname,"r")
    msgzip = MIMEBase('application', "zip")
    msgzip.set_payload(ziptemp.read())
    encoders.encode_base64(msgzip)
    msgzip.add_header('Content-Disposition', 'attachment', filename=filenamezip)
    ziptemp.close()

    msg.attach(msgzip)

    #delete created files
    os.remove(ziptempname)
    os.remove(tempname)


    #print "******************\r\n"
    #print msg.as_string()
    #print "******************\r\n"

    s = smtplib.SMTP(mailSmtp)
    # send to IP owners first
    if msg["To"] != "":
        toList = msg["To"].split(",")
        s.sendmail(msg["From"], toList, msg.as_string())
    # send a copy
    reportEmail=reportEmailCc
    if spam:
        reportEmail=reportEmailSpamCc
    if reportEmail != "":
        toList = reportEmail.split(",")
        for emailAddress in toList:
            if msg.has_key("To"):
                msg.replace_header("To",str(emailAddress))
            else:
                msg["To"]=str(emailAddress)
            s.sendmail(msg["From"], emailAddress, msg.as_string())
            
    s.quit()


@app.before_request
def before_request():
    g.db=MySQLdb.connect(host=dbHost,user=dbUser,passwd=dbPassword,db=dbName,charset = "utf8",use_unicode = True)
    g.db.autocommit(True)
    is_authorized()

@app.teardown_request
def teardown_request(exception):
    g.db.close()

@app.route('/')
def home():
    title = "Home"
    return render_template('home.html',title=title)

@app.route('/email/id/<int:emailId>')
def displayMessage(emailId):
    strSql="select content from arfEmail where emailId=%s" % emailId
    g.db.query(strSql)
    result = g.db.store_result()
    if result is not None:
        row = result.fetch_row(1,1)[0]
        content = row['content']
    else:
        content = "No email found for emailId=%s" % emailId
    return Response(content, mimetype='text/plain')

@app.route('/url/')
@app.route('/url/pattern/')
@app.route('/url/pattern/<pattern>')
@app.route('/url/pattern/<pattern>/limit/')
@app.route('/url/pattern/<pattern>/limit/<int:limit>')
@app.route('/url/pattern/<pattern>/days/<int:days>')
@app.route('/url/pattern/<pattern>/days/<int:days>/daysago/<int:daysago>')
@app.route('/url/days/<int:days>')
@app.route('/url/days/<int:days>/daysago/<int:daysago>')
def url(pattern="%",limit=50,days=0,daysago=0):
    strSqlDate = ''
    strSqlLimit = ''
    titleDate = ''
    titleLimit = ''
    if days>0:
        today = datetime.utcnow()
        today = today.date()
        firstday = today - timedelta(days+daysago)
        lastday = today - timedelta(daysago)
        strSqlDate = 'lastSeen >="%s" and lastSeen <="%s 23:59:59" and ' % (firstday.strftime('%Y-%m-%d'),lastday.strftime('%Y-%m-%d'))
        titleDate = ' %s - %s UTC ' % (firstday.strftime('%Y-%m-%d'),lastday.strftime('%Y-%m-%d'))
        limit = 0

    if limit>0:
        strSqlLimit = 'limit %s' % limit
        titleLimit = 'limit %s' % limit

    strSql='select urlId, firstSeen, lastSeen, INET_NTOA(urlIp) as Ip, urlAsn, url from url where %s url like "%s" order by lastSeen desc %s' % (strSqlDate, pattern, strSqlLimit)
    cur = g.db.cursor()
    cur.execute(strSql)
    entries = [dict(urlId=row[0], firstSeen=row[1], lastSeen=row[2], Ip=row[3], urlAsn=row[4], url=row[5]) for row in cur.fetchall()]
    cur.close()
    title = "URLs with the pattern '%s' %s%s" % (pattern, titleDate, titleLimit)
    return render_template('url_list.html', entries=entries, title=title)

@app.route('/url/subject/pattern/<pattern>')
def urllistSubject(pattern="%"):
    strSql='select distinct c.urlId as urlId, c.firstSeen, c.lastSeen, INET_NTOA(c.urlIp) as Ip, c.urlAsn as urlAsn, c.url as url from arfEmail a, emailUrl b, url c where a.emailId=b.emailId and b.urlId = c.urlId and a.subject like "%s" order by c.lastSeen desc' % pattern
    cur = g.db.cursor()
    cur.execute(strSql)
    entries = [dict(urlId=row[0], firstSeen=row[1], lastSeen=row[2], Ip=row[3], urlAsn=row[4], url=row[5]) for row in cur.fetchall()]
    cur.close()
    title = "URLs from emails with a subject containing %s" % pattern
    return render_template('url_list.html', entries=entries, title=title)

@app.route('/email/')
@app.route('/email/type/')
@app.route('/email/type/<emailType>')
@app.route('/email/type/<emailType>/limit/')
@app.route('/email/type/<emailType>/limit/<int:limit>')
@app.route('/email/type/<emailType>/days/<int:days>')
@app.route('/email/type/<emailType>/days/<int:days>/daysago/<int:daysago>')
@app.route('/email/days/<int:days>')
@app.route('/email/days/<int:days>/daysago/<int:daysago>')
def displayMailList(emailType=None,limit=50,days=0,daysago=0):
    strSqlDate = ''
    strSqlLimit = ''
    titleDate = ''
    titleLimit = ''
    if days>0:
        today = datetime.utcnow()
        today = today.date()
        firstday = today - timedelta(days+daysago)
        lastday = today - timedelta(daysago)
        strSqlDate = 'arrivalDate >="%s" and arrivalDate <="%s 23:59:59" and ' % (firstday.strftime('%Y-%m-%d'),lastday.strftime('%Y-%m-%d'))
        titleDate = ' %s - %s UTC ' % (firstday.strftime('%Y-%m-%d'),lastday.strftime('%Y-%m-%d'))
        limit = 0

    if limit>0:
        strSqlLimit = 'limit %s' % limit
        titleLimit = 'limit %s' % limit

    strSqlEmailType=""
    if emailType is not None:
        if emailType=="normal" or emailType=="bounce" or emailType=="auto-replied":
            strSqlEmailType='and emailType="%s"' % emailType
        if emailType=="reported":
            strSqlEmailType='and reported!=0'   
    strSql='select e.emailId as emailId, reported, arrivalDate, d.domain as reportedDomain, INET_NTOA(sourceIp) as sourceIp, f.domain as sourceDomain, deliveryResult, subject from arfEmail e, domain d, domain f where %s e.reportedDomainID=d.domainId and e.sourceDomainId=f.domainId %s order by emailId desc %s' % (strSqlDate, strSqlEmailType, strSqlLimit)
    cur = g.db.cursor()
    cur.execute(strSql)
    entries = [dict(emailId=row[0], reported=row[1], arrivalDate=row[2], reportedDomain=row[3], sourceIp=row[4], sourceDomain=row[5], dnsbl="", deliveryResult=row[6], subject=row[7]) for row in cur.fetchall()]
    cur.close()
    entries = addDnsbl(entries)
    title = "Email List%s%s" % (titleDate,titleLimit) 
    return render_template('mail_list.html', entries=entries, title=title)

@app.route('/email/asn/')
@app.route('/email/asn/<int:asn>')
@app.route('/email/asn/<int:asn>/type/')
@app.route('/email/asn/<int:asn>/type/<emailType>')
@app.route('/email/asn/<int:asn>/type/<emailType>/days/<int:days>')
@app.route('/email/asn/<int:asn>/type/<emailType>/days/<int:days>/<int:daysago>')
@app.route('/email/asn/<int:asn>/days/<int:days>')
@app.route('/email/asn/<int:asn>/days/<int:days>/daysago/<int:daysago>')
def displayAsnList(asn=0,emailType=None,limit=50,days=0,daysago=0):
    strSqlDate = ''
    strSqlLimit = ''
    titleDate = ''
    titleLimit = ''
    if days>0:
        today = datetime.utcnow()
        today = today.date()
        firstday = today - timedelta(days+daysago)
        lastday = today - timedelta(daysago)
        strSqlDate = 'arrivalDate >="%s" and arrivalDate <="%s 23:59:59" and ' % (firstday.strftime('%Y-%m-%d'),lastday.strftime('%Y-%m-%d'))
        titleDate = ' %s - %s UTC ' % (firstday.strftime('%Y-%m-%d'),lastday.strftime('%Y-%m-%d'))
        limit = 0

    if limit>0:
        strSqlLimit = 'limit %s' % limit
        titleLimit = 'limit %s' % limit
    titleAsn = ' for AS%s - ' % asn

    strSqlEmailType=""
    if emailType is not None:
        if emailType=="normal" or emailType=="bounce" or emailType=="auto-replied":
            strSqlEmailType='and emailType="%s"' % emailType
        if emailType=="reported":
            strSqlEmailType='and reported!=0'
    strSql='select e.emailId as emailId, reported, arrivalDate, d.domain as reportedDomain, INET_NTOA(sourceIp) as sourceIp, f.domain as sourceDomain, deliveryResult, subject from arfEmail e, domain d, domain f where %s e.reportedDomainID=d.domainId and e.sourceDomainId=f.domainId %s and e.sourceAsn=%s order by emailId desc %s' % (strSqlDate, strSqlEmailType, asn, strSqlLimit)
    cur = g.db.cursor()
    cur.execute(strSql)
    entries = [dict(emailId=row[0], reported=row[1], arrivalDate=row[2], reportedDomain=row[3], sourceIp=row[4], sourceDomain=row[5], dnsbl="", deliveryResult=row[6], subject=row[7]) for row in cur.fetchall()]
    cur.close()
    entries = addDnsbl(entries)
    title = "Email List%s%s%s" % (titleAsn,titleDate,titleLimit)
    return render_template('mail_list.html', entries=entries, title=title)

@app.route('/email/urlId/<int:urlId>')
def displayMailListFromUrl(urlId=0):
    strSql='select distinct e.emailId as emailId, reported, arrivalDate, d.domain as reportedDomain, INET_NTOA(sourceIp) as sourceIp, f.domain as sourceDomain, deliveryResult, subject from arfEmail e, domain d, domain f, emailUrl g where e.reportedDomainID=d.domainId and e.sourceDomainId=f.domainId and e.emailId=g.emailId and g.urlId=%s order by emailId desc' % (urlId)
    cur = g.db.cursor()
    cur.execute(strSql)
    entries = [dict(emailId=row[0], reported=row[1], arrivalDate=row[2], reportedDomain=row[3], sourceDomain=row[4], deliveryResult=row[5], subject=row[6]) for row in cur.fetchall()]
    cur.close()
    title = "Emails containing an url"
    return render_template('mail_list.html', entries=entries, title=title)

@app.route('/email/subject/')
@app.route('/email/subject/<subject>')
@app.route('/email/subject/<subject>/limit/')
@app.route('/email/subject/<subject>/limit/<int:limit>')
@app.route('/email/subject/<subject>/days/<int:days>')
@app.route('/email/subject/<subject>/days/<int:days>/daysago/<int:daysago>')
def displayMailListSubject(subject="%",limit=50,days=0,daysago=0):
    strSqlDate = ''
    strSqlLimit = ''
    titleDate = ''
    titleLimit = ''
    if days>0:
        today = datetime.utcnow()
        today = today.date()
        firstday = today - timedelta(days+daysago)
        lastday = today - timedelta(daysago)
        strSqlDate = 'arrivalDate >="%s" and arrivalDate <="%s 23:59:59" and ' % (firstday.strftime('%Y-%m-%d'),lastday.strftime('%Y-%m-%d'))
        titleDate = ' %s - %s UTC ' % (firstday.strftime('%Y-%m-%d'),lastday.strftime('%Y-%m-%d'))
        limit = 0

    if limit>0:
        strSqlLimit = 'limit %s' % limit
        titleLimit = 'limit %s' % limit

    strSql='select distinct e.emailId as emailId, reported, arrivalDate, d.domain as reportedDomain, INET_NTOA(sourceIp) as sourceIp, f.domain as sourceDomain, deliveryResult, subject from arfEmail e, domain d, domain f where %s e.reportedDomainID=d.domainId and e.sourceDomainId=f.domainId and e.subject like "%s" order by emailId desc %s' % (strSqlDate, subject, strSqlLimit)
    cur = g.db.cursor()
    cur.execute(strSql)
    entries = [dict(emailId=row[0], reported=row[1], arrivalDate=row[2], reportedDomain=row[3], sourceIp=row[4], sourceDomain=row[5], dnsbl="", deliveryResult=row[6], subject=row[7]) for row in cur.fetchall()]
    cur.close()
    entries = addDnsbl(entries)
    title = "Emails with a subject containing %s%s%s" % (subject,titleDate,titleLimit) 
    return render_template('mail_list.html', entries=entries, title=title)

@app.route('/email/url/pattern/')
@app.route('/email/url/pattern/<pattern>')
@app.route('/email/url/pattern/<pattern>/limit/')
@app.route('/email/url/pattern/<pattern>/limit/<int:limit>')
@app.route('/email/url/pattern/<pattern>/days/<int:days>')
@app.route('/email/url/pattern/<pattern>/days/<int:days>/daysago/<int:daysago>')
def displayMailListUrl(pattern="%",limit=50,days=0,daysago=0):
    strSqlDate = ''
    strSqlLimit = ''
    titleDate = ''
    titleLimit = ''
    if days>0:
        today = datetime.utcnow()
        today = today.date()
        firstday = today - timedelta(days+daysago)
        lastday = today - timedelta(daysago)
        strSqlDate = 'and e.arrivalDate >="%s" and e.arrivalDate <="%s 23:59:59" ' % (firstday.strftime('%Y-%m-%d'),lastday.strftime('%Y-%m-%d'))
        titleDate = ' %s - %s UTC ' % (firstday.strftime('%Y-%m-%d'),lastday.strftime('%Y-%m-%d'))
        limit = 0

    if limit>0:
        strSqlLimit = 'limit %s' % limit
        titleLimit = ' limit %s' % limit

    strSql='select distinct e.emailId as emailId, reported, arrivalDate, d.domain as reportedDomain, INET_NTOA(sourceIp) as sourceIp, f.domain as sourceDomain, deliveryResult, subject from arfEmail e, domain d, domain f, emailUrl g, url h where e.reportedDomainID=d.domainId and e.sourceDomainId=f.domainId and e.emailId=g.emailId and g.urlId=h.urlId and h.url like "%s" %s order by emailId desc %s' % (pattern,strSqlDate,strSqlLimit)
    cur = g.db.cursor()
    cur.execute(strSql)
    entries = [dict(emailId=row[0], reported=row[1], arrivalDate=row[2], reportedDomain=row[3], sourceIp=row[4], sourceDomain=row[5], dnsbl="", deliveryResult=row[6], subject=row[7]) for row in cur.fetchall()]
    cur.close()
    entries = addDnsbl(entries)
    title = "Emails that contains a url with the pattern %s%s%s" % (pattern,titleDate,titleLimit)
    return render_template('mail_list.html', entries=entries, title=title)

@app.route('/email/file/pattern/')
@app.route('/email/file/pattern/<pattern>')
@app.route('/email/file/pattern/<pattern>/limit/')
@app.route('/email/file/pattern/<pattern>/limit/<int:limit>')
@app.route('/email/file/pattern/<pattern>/days/<int:days>')
@app.route('/email/file/pattern/<pattern>/days/<int:days>/daysago/<int:daysago>')
def displayMailListFile(pattern="%",limit=50,days=0,daysago=0):
    strSqlDate = ''
    strSqlLimit = ''
    titleDate = ''
    titleLimit = ''
    if days>0:
        today = datetime.utcnow()
        today = today.date()
        firstday = today - timedelta(days+daysago)
        lastday = today - timedelta(daysago)
        strSqlDate = 'and e.arrivalDate >="%s" and e.arrivalDate <="%s 23:59:59" ' % (firstday.strftime('%Y-%m-%d'),lastday.strftime('%Y-%m-%d'))
        titleDate = ' %s - %s UTC ' % (firstday.strftime('%Y-%m-%d'),lastday.strftime('%Y-%m-%d'))
        limit = 0

    if limit>0:
        strSqlLimit = 'limit %s' % limit
        titleLimit = ' limit %s' % limit

    strSql='select distinct e.emailId as emailId, reported, arrivalDate, d.domain as reportedDomain, INET_NTOA(sourceIp) as sourceIp, f.domain as sourceDomain, deliveryResult, subject from arfEmail e, domain d, domain f, emailFile g, file h where e.reportedDomainID=d.domainId and e.sourceDomainId=f.domainId and e.emailId=g.emailId and g.fileId=h.fileId and h.filename like "%s" %s order by emailId desc %s' % (pattern,strSqlDate,strSqlLimit)
    cur = g.db.cursor()
    cur.execute(strSql)
    entries = [dict(emailId=row[0], reported=row[1], arrivalDate=row[2], reportedDomain=row[3], sourceIp=row[4], sourceDomain=row[5], dnsbl="", deliveryResult=row[6], subject=row[7]) for row in cur.fetchall()]
    cur.close()
    entries = addDnsbl(entries)
    title = "Emails that contains a file with the pattern %s%s%s" % (pattern,titleDate,titleLimit)
    return render_template('mail_list.html', entries=entries, title=title)

@app.route('/email/graph')
@app.route('/email/graph/live')
def emailGraph():
    strSql = 'select DATE_FORMAT(arrivalDate,"%Y/%m/%d %H") as hour, emailType, count(emailId) as total from arfEmail where arrivalDate >= DATE_SUB(CURDATE(),INTERVAL 72 HOUR) group by hour, emailType order by hour,emailType'
    cur = g.db.cursor()
    cur.execute(strSql)
    data = [dict(hour=row[0], emailType=row[1], total=row[2]) for row in cur.fetchall()]
    cur.close()
    entries = []
    oldHour=data[0]['hour']
    normal=0
    bounce=0
    autoreplied=0
    for item in data:
        if item['hour']!=oldHour:
            entries.append(dict(hour=oldHour[5:], normal=normal, bounce=bounce, autoreplied=autoreplied))
            normal=0
            bounce=0
            autoreplied=0
            oldHour=item['hour']
        if item['emailType'] == "normal":
            normal = item['total']
        if item['emailType'] == "bounce":
            bounce = item['total']
        if item['emailType'] =="auto-replied":
            autoreplied = item['total']
    try:
        entries.append(dict(hour=item['hour'][5:], normal=normal, bounce=bounce, autoreplied=autoreplied))
    except:
        pass
    title = "Email bar graph"
    return render_template('email_graph.html', entries=entries, title=title)

@app.route('/email/graph/reported')
def emailGraphReported():
    strSql = 'select DATE_FORMAT(arrivalDate,"%Y/%m/%d") as day, count(emailId) as total from arfEmail where arrivalDate >= DATE_SUB(CURDATE(),INTERVAL 90 DAY) and reported=True group by day order by day'
    cur = g.db.cursor()
    cur.execute(strSql)
    data = [dict(day=row[0], total=row[1]) for row in cur.fetchall()]
    cur.close()
    entries = []
    total=0
    d = datetime.strptime(data[0]['day'],'%Y/%m/%d')
    delta = timedelta(days=1)
    while d <= datetime.utcnow():
        day=d.strftime("%Y/%m/%d")
        day2=d.strftime("%Y/%m/%d %a")
        total=0
        for item in data:
            if item['day']==day:
                total=item['total']
                break
        try:
            entries.append(dict(day=day2, total=total))
        except:
            pass
        d += delta
    title = "Reported Email bar graph"
    return render_template('email_graph_reported.html', entries=entries, title=title)

@app.route('/email/map')
@app.route('/email/map/days/<int:days>')
@app.route('/email/map/days/<int:days>/daysago/<int:daysago>')
def emailMap(days=7,daysago=0):
    today = datetime.utcnow()
    today = today.date()
    firstday = today - timedelta(days+daysago)
    lastday = today - timedelta(daysago)

    strSql = 'select countryCode, count(emailId) as total from arfEmail where arrivalDate >="%s" and arrivalDate <="%s 23:59:59" and reported=True group by countryCode;'  % (firstday.strftime('%Y-%m-%d'),lastday.strftime('%Y-%m-%d'))
    cur = g.db.cursor()
    cur.execute(strSql)
    entries = [dict(countryCode=row[0], total=row[1]) for row in cur.fetchall()]
    maxTotal = 0
    reportedTotal=0
    for entry in entries:
        reportedTotal=reportedTotal+entry['total']
        if entry['total']>maxTotal:
            maxTotal=entry['total']
    cur.close()

    strSql = 'select sourceAsn, count(emailId) as total from arfEmail where arrivalDate >="%s" and arrivalDate <="%s 23:59:59" and reported=True group by sourceAsn order by total desc limit 20;'  % (firstday.strftime('%Y-%m-%d'),lastday.strftime('%Y-%m-%d'))
    cur = g.db.cursor()
    cur.execute(strSql)
    entriesAsn = []
    for row in cur.fetchall():
        (asn,countryCode,rir,createDate,name)=getAsnInfo(row[0])
        abuseAsn=getEmailAbuseFromAsn(asn)
        entriesAsn.append(dict(sourceAsn=row[0], total=row[1], countryCode=countryCode, createDate=createDate, name=name, abuseAsn=abuseAsn))
    cur.close()

    title = 'Reported Emails Map %s - %s UTC' % (firstday.strftime('%Y-%m-%d'),lastday.strftime('%Y-%m-%d'))
    #return render_template('email_map.html', entries=entries, title=title)
    return render_template('email_map.html', entries=entries, maxTotal=maxTotal, reportedTotal=reportedTotal, entriesAsn = entriesAsn, title=title, days=days)

@app.route('/reportemail',methods=['GET','POST'])
def reportEmail():
    emailList = []
    nbEmailReported=0
    reporting=False
    title = "Reporting emails"
    for item in request.form:
        if item[:7]=="emailid":
            emailList.append(request.form[item])
        if item=="submit" and request.form[item]=="Send Reports":
            reporting=True
            Title = "Emails reported"
            
    strEmailList = ", ".join(emailList)
    strSql = 'select distinct e.emailId as emailId, reported, arrivalDate, d.domain as reportedDomain, INET_NTOA(e.sourceIp) as sourceIp, sourceAsn, f.domain as sourceDomain, deliveryResult, subject, content from arfEmail e, domain d, domain f where e.reportedDomainID=d.domainId and e.sourceDomainId=f.domainId and emailId in (%s)' % strEmailList
    cur = g.db.cursor()
    cur.execute(strSql)
    entries = [dict(emailId=row[0], reported=row[1], arrivalDate=row[2], reportedDomain=row[3], sourceIp=row[4], sourceAsn=row[5], sourceDomain=row[6], deliveryResult=row[7], subject=row[8], content=row[9], emailAbuse="", urlList="") for row in cur.fetchall()]
    cur.close()
    for item in entries:
        #add the list of urls
        strSql='select distinct c.url as url from arfEmail a, emailUrl b, url c where a.emailId=b.emailId and b.urlId = c.urlId and a.emailId=%s' % item['emailId']
        cur = g.db.cursor()
        cur.execute(strSql)
        urlList = [row[0] for row in cur.fetchall()]
        cur.close()
        item['urlList'] = urlList

        #find where to report abuse
        item['emailAbuse']=getEmailAbuseFromIp(item['sourceIp'])
        abuseAsn=getEmailAbuseFromAsn(item['sourceAsn'])
        if abuseAsn!="" and item['emailAbuse'].find(abuseAsn)<0:
            item['emailAbuse']=item['emailAbuse']+','+abuseAsn
            
        if reporting:
            sendArf(item=item,spam=False)
            strSql = 'update arfEmail set reported=1 where emailId=%s' % item['emailId']
            cur = g.db.cursor()
            cur.execute(strSql)
            cur.close()
            item['reported'] = 1
            nbEmailReported = nbEmailReported+1
    if reporting:
        flash(str(nbEmailReported)+" emails have been reported to the abuse handle of each IP")
    return render_template('report_email.html', entries=entries, title=title)

@app.route('/reportspam',methods=['GET','POST'])
def reportSpam():
    global privatenet
    analyze=False
    reporting=False
    strEmail=""
    strSourceIpList=""
    ipList = []
    urls = []
    listUrl =[]
    abuseList = []
    abuseEmailList = {}
    urlListId = []
    subject = ""
    match_ip = re.compile(r'.*\[(((25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\.){3}(25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)).*')
    match_url = re.compile(r"""(?i)\b((?:https?://|www\d{0,3}[.]|[a-z0-9.\-]+[.][a-z]{2,4}/)(?:[^\s()<>]+|\(([^\s()<>]+|(\([^\s()<>]+\)))*\))+(?:\(([^\s()<>]+|(\([^\s()<>]+\)))*\)|[^\s`!()\[\]{};:'".,<>?«»“”‘’]))""", re.DOTALL)
    title = "send ARF"
    for item in request.form:
        if item[:7]=="abuseid":
            abuseList.append(int(item[7:]))
        if item[:10]=="abuseemail":
            abuseEmailList[int(item[10:])]=request.form[item]
        if item[:5]=="urlid":
            urlListId.append(int(item[5:]))
        if item=="email":
            strEmail=request.form[item]
        if item=="submit" and request.form[item]=="Analyse email":
            analyze=True
            Title = " Analyzed email"
        if item=="submit" and request.form[item]=="Send ARF":
            reporting=True
            Title = "ARF Report sent" 

    if analyze or reporting:
        ipRawList=match_ip.findall(strEmail)
        u_strEmail=strEmail.encode('ascii','replace')
        msg = email.message_from_string(u_strEmail) 
        subject=msg.get('Subject')
        arrivalDate=msg.get('Date')

        for part in msg.walk():
            ctype = part.get_content_type()
            if part.get_content_maintype() == 'text':
                orgmsgpart = part.get_payload(decode=True)

                #signal.signal(signal.SIGALRM, handleTimeOut)
                #signal.alarm(30)
                try:
                    urls= urls + match_url.findall(orgmsgpart)
                except Exception, err:
                    print ' A error: %s with %s' % (str(err),orgmsgpart)
                    #signal.alarm(0)

        for url in urls:
            listUrl.append(url[0])
        ipUniqueList = []
        for ip in ipRawList:
            if ip[0] not in ipUniqueList:
                ipUniqueList.append(ip[0])

        for ip in ipUniqueList:
            if not addressInNetworkList(ip,privatenet):
                abuseEmail=getEmailAbuseFromIp(ip)
                asn=getIp4ToAsn(ip)
                abuseAsn=getEmailAbuseFromAsn(asn)
                (resAsn,countryCode,rir,createDate,asnName)=getAsnInfo(asn)
                if abuseAsn!="":
                    abuseEmail=abuseEmail+","+abuseAsn
                ipList.append(dict(ip=ip,email=abuseEmail,asn=asn,asnName=asnName,asnCountryCode=countryCode))

    if reporting:
        emailList=[]
        sourceIpList=[]
        urlList =[]
        for id in abuseList:
            emailEntryList=abuseEmailList[id].split(",")
            for emailAddress in emailEntryList:
                emailList.append(emailAddress)
            sourceIpList.append(ipList[id-1]["ip"])
        emailUniqueList = []
        for id in urlListId:
            urlList.append(listUrl[id-1])
        for emailAddress in emailList:
            if emailAddress not in emailUniqueList:
                emailUniqueList.append(emailAddress) 
        sourceIpUniqueList = []
        for sourceIp in sourceIpList:
            if sourceIp not in sourceIpUniqueList:
                sourceIpUniqueList.append(sourceIp)
        strEmailList = ", ".join(emailUniqueList)
        strSourceIpList = ", ".join(sourceIpUniqueList)

        item = dict(sourceIp=strSourceIpList, arrivalDate=arrivalDate, subject=subject, content=u_strEmail, emailAbuse=strEmailList, urlList=urlList)
        sendArf(item=item,spam=True)

        flash("this email has been reported to the abuse handle of each IP")
    return render_template('arf.html', iplist=ipList, urls=listUrl, subject=subject, email=strEmail, analyze=analyze, reporting=reporting, title=title)

if __name__ == '__main__':
    title = "Lafayette"
    app.run(host='0.0.0.0',debug=True)
