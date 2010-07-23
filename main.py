import os
import hashlib
import base64
import urllib
import logging
import feedparser
from google.appengine.api import xmpp
from google.appengine.ext import webapp
from google.appengine.ext.webapp.util import run_wsgi_app
from google.appengine.ext.webapp import xmpp_handlers
from google.appengine.ext.webapp import template
from google.appengine.ext import db
from google.appengine.api import urlfetch
from google.appengine.api.labs import taskqueue
from google.appengine.api import memcache


SUPERFEEDR_LOGIN = ""
SUPERFEEDR_PASSWORD = ""

##
# the function that sends subscriptions/unsubscriptions to Superfeedr
def superfeedr(mode, keyword):
  post_data = {
      'hub.mode' : mode,
      'hub.callback' : "http://supertrackr.appspot.com/hubbub/" + keyword.key().name(),
      'hub.topic' : keyword.feed, 
      'hub.verify' : 'sync',
      'hub.verify_token' : '',
  }
  base64string = base64.encodestring('%s:%s' % (SUPERFEEDR_LOGIN, SUPERFEEDR_PASSWORD))[:-1]
  form_data = urllib.urlencode(post_data)
  result = urlfetch.fetch(url="http://superfeedr.com/hubbub",
                  payload=form_data,
                  method=urlfetch.POST,
                  headers={"Authorization": "Basic "+ base64string, 'Content-Type': 'application/x-www-form-urlencoded'},
                  deadline=10)
  logging.info('Result of %s to %s => %s (%d)',mode, keyword.feed, result.content, result.status_code )
  
  return result

def get_or_add_keyword(keyword_value):
    logging.info('#Getting keyword '+keyword_value)
    try:
        query = Keyword.all()
        query.filter('keyword =', keyword_value)
        keyword = query[0]
        logging.debug('#keyword found')
    except:
        logging.debug('#instantiating keyword '+keyword_value)
        key = hashlib.sha224(keyword_value).hexdigest()
        keyword = Keyword(key_name=key, keyword=keyword_value)
        keyword.keyword = keyword_value
        keyword.feed = "http://superfeedr.com/track/"+keyword_value+"/"
        keyword.save()
        result = superfeedr("subscribe", keyword)
    return keyword
    
 
def track_keyword(keyword_value, jid):
    logging.debug("getting jid")
    logging.debug(jid)
    key = hashlib.sha224(keyword_value + jid).hexdigest()
    keyword = get_or_add_keyword(keyword_value)
    subscription = Subscription(key_name=key, keyword=keyword, jid=jid)
    subscription.put() # saves the subscription

def remove_track_keyword(keyword_value, jid):
    logging.info("getting jid")
    logging.info(jid)
    subscription_key = hashlib.sha224(keyword_value + jid).hexdigest()
    try:
        subscription = Subscription.get_by_key_name(subscription_key)
        keyword = subscription.keyword
        subscription.delete()
        keywords = keyword.subscribers.fetch(10)
        if len(keywords)==0:
            result = superfeedr("unsubscribe", keyword)
            keyword.delete()
    except:
        pass

    #print keyword
    #subscription = Subscription(key_name=key, keyword=keyword, jid=jid)
    #subscription.put() # saves the subscription

class Keyword(db.Model):
  keyword = db.StringProperty()
  feed = db.LinkProperty()
  created_at = db.DateTimeProperty(auto_now_add=True)

# The subscription model that matches a feed and a jid.
class Subscription(db.Model):
  keyword = db.ReferenceProperty(Keyword, collection_name='subscribers')
  jid = db.StringProperty(required=True)
  created_at = db.DateTimeProperty(required=True, auto_now_add=True)

##
# The web app interface
class MainPage(webapp.RequestHandler):
  
  def Render(self, template_file, template_values = {}):
     path = os.path.join(os.path.dirname(__file__), 'templates', template_file)
     self.response.out.write(template.render(path, template_values))
  
  def get(self):
    self.Render("index.html")

  def post(self):
    jid = self.request.get('jid')
    if jid.find("@") == -1:
        self.response.headers['Content-Type'] = 'text/plain'
        self.response.out.write(jid + " doesn't seem to be a valid jabber address")
    else:
        msg = "Welcome to supertrackr@appspot.com. You can track all sorts of keywords and what not. \n\nStart by sending:\n/track <keyword>"
        invite = xmpp.send_invite(jid)
        status_code = xmpp.send_message(jid, msg)
        logging.info("--")
        logging.info(status_code)
        logging.info("--")
        self.response.headers['Content-Type'] = 'text/plain'
        self.response.out.write('message sent to ' + jid + '.<br /> you will receive an IM from supertrackr@appspot.com')
        self.response.out.write(status_code)
    

# The web app interface
class FeedReceiver(webapp.RequestHandler):
  def post(self):
    feed_sekret = self.request.get('feed_sekret')
    mem_key = self.request.get('mem_key')
    feed_body = memcache.get(mem_key)
    data = feedparser.parse(feed_body)
    keyword = Keyword.get_by_key_name(feed_sekret)
    try:
        logging.info('Found %d entries in %s', len(data.entries), keyword.feed)
        for entry in data.entries:
            title =  entry.get('title', '')
            link = entry.get('link', '')
            post_params = {
                'link': link,
                'title' : title,
                'feed_sekret': feed_sekret
            }
            logging.info('Found entry with title = "%s", link = "%s"', title, link)
            logging.debug('sending on to track queue')

            taskqueue.Task(url='/api/track_receiver', params=post_params).add(queue_name='apiwork')
    except:
        pass

# The web app interface
class TrackResponder(webapp.RequestHandler):
  
    def post(self):
        user_address = self.request.get('user_address')
        msg = self.request.get('msg')
        status_code = xmpp.send_message(user_address, msg)
        pass

# The web app interface
class TrackReceiver(webapp.RequestHandler):
  
  def post(self):
    link = self.request.get('link')
    title = self.request.get('title')
    feed_sekret = self.request.get('feed_sekret')

    #do bitly stuff here
    keyword = Keyword.get_by_key_name(feed_sekret)
    try:
        subscribers = keyword.subscribers
        for subscription in subscribers:
            user_address = subscription.jid
            msg = title + "\n" + link

            post_params = {
                    "msg":msg,
                    "user_address":user_address,
            }
            taskqueue.Task(url='/api/track_responder', params=post_params).add(queue_name='trackmessages')
            logging.debug(post_params)
    except:
        pass

# The HubbubSusbcriber
class HubbubSubscriber(webapp.RequestHandler):

  ##
  # Called upon notification
  def post(self, feed_sekret):
    keyword = Keyword.get_by_key_name(feed_sekret)
    if(keyword == None):
      self.response.set_status(404)
      self.response.out.write("Sorry, no feed."); 
      
    else:
      key = hashlib.sha224(self.request.body).hexdigest()
      memcache.add(key, self.request.body)
      post_params = {
            "feed_sekret": feed_sekret,
            "mem_key": key,
            
            }
      taskqueue.Task(url='/api/feed_receiver', params=post_params).add(queue_name='feedreceiver')
      self.response.set_status(200)
      self.response.out.write("Aight. Saved."); 
  
  def get(self, feed_sekret):
    self.response.out.write(self.request.get('hub.challenge'))
    self.response.set_status(200)
  
##
# The XMPP App interface
class XMPPHandler(xmpp_handlers.CommandHandler):
  
  # Asking to subscribe to a feed
  def track_command(self, message=None):
    message = xmpp.Message(self.request.POST)
    logging.debug(message.sender)
    subscriber = message.sender#.rpartition("/")[0]
    track_keyword(message.arg, subscriber)
    message.reply("Well done! You're now tracking " + message.arg)
    
  ##
  # Asking to unsubscribe to a feed
  def remove_command(self, message=None):
    message = xmpp.Message(self.request.POST)
    subscriber = message.sender#.rpartition("/")[0]
    remove_track_keyword(message.arg, subscriber)
    #subscription = Subscription.get_by_key_name(hashlib.sha224(message.arg + subscriber).hexdigest())
    #result = superfeedr("unsubscribe", subscription)
    #subscription.delete() # saves the subscription
    message.reply("REMOVED!! You're no longer tracking " + message.arg)

  ##
  # List subscriptions by page
  # 10/page
  # page default to 1
  def ls_command(self, message=None):
    message = xmpp.Message(self.request.POST)
    subscriber = message.sender#.rpartition("/")[0]
    query = Subscription.all().filter("jid =",subscriber).order("-created_at")
    count = query.count()
    if count == 0:
      message.reply("Seems like you are not tracking any keywords yet. Type\n  /track superfeedr\nto play around.")
    else:
      page_index = int(message.arg or 1)
      if count%10 == 0:
        pages_count = count/10
      else:
        pages_count = count/10 + 1
    
      page_index = min(page_index, pages_count)
      offset = (page_index - 1) * 10 
      subscriptions = query.fetch(10, offset)
      message.reply("Your have %d tracked keywords in total: page %d/%d \n" % (count,page_index,pages_count))
      feed_list = [s.keyword.keyword for s in subscriptions]
      message.reply("\n".join(feed_list))

  ##
  # Asking for help
  def hello_command(self, message=None):
    message = xmpp.Message(self.request.POST)
    message.reply("For more info, type /help.")
  
  ##
  # Asking for help
  def help_command(self, message=None):
    message = xmpp.Message(self.request.POST)
    help_msg = "" \
           "/track <keyword>\n/remove <keyword> -> subscribe or unsubscribe to keywords \n\n" \
           "/ls ->  list tracked keywords \n\n" \
           "/help ->  get help message\n\n" \
           "Warning: Tracking lots of keywords or generic keywords will *blow your shit up*!\n"
    message.reply(help_msg)
    message.reply(message.body)
  
  ##
  # All other commants
  def unhandled_command(self, message=None):
    message = xmpp.Message(self.request.POST)
    message.reply("Please type /help for help.")
  
route = [
        ('/_ah/xmpp/message/chat/', XMPPHandler), 
        ('/', MainPage), 
        ('/hubbub/(.*)', HubbubSubscriber),
        ('/api/track_responder', TrackResponder),
        ('/api/track_receiver', TrackReceiver),
        ('/api/feed_receiver', FeedReceiver),
        ]
application = webapp.WSGIApplication(route,debug=True)

def main():
  run_wsgi_app(application)

if __name__ == "__main__":
  main()
  
