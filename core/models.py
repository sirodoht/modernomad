from django.contrib.auth.models import User
from django.core.files.storage import FileSystemStorage
from django.db import models
from django.contrib.sites.models import Site
from django.core import urlresolvers
from PIL import Image
import os, datetime
from django.conf import settings
from django.core.files.storage import default_storage
import uuid
import stripe
from django.db.models import Q

# imports for signals
import django.dispatch 
from django.dispatch import receiver
from django.db.models.signals import pre_save, post_save

# mail imports
from django.core.mail import EmailMultiAlternatives
from django.template.loader import get_template
from django.template import Context


# there is a weird db issue it seems with setting a field to null=False after it has been defined as null=True. 
# see http://od-eon.com/blogs/stefan/adding-not-null-column-south/ and
# http://south.aeracode.org/ticket/782
# one suggestion was to try setting default value in the model file, but this hasn't worked either. 
# currently the field are still set to null=True, though they shouldn't be. 

def default_location():
	return Location.objects.get(pk=1)

class Location(models.Model):
	name = models.CharField(max_length=200)
	slug = models.CharField(max_length=60, unique=True, help_text="Try to make this short and sweet. It will also be used to form several location-specific email addresses in the form of xxx@<your_slug>.mail.embassynetwork.com")
	short_description = models.TextField()
	about_page = models.TextField()
	address = models.CharField(max_length=300)
	stay_page = models.TextField()
	front_page_stay = models.TextField()
	front_page_participate = models.TextField()
	max_reservation_days = models.IntegerField(default=14)
	welcome_email_days_ahead = models.IntegerField(default=2)
	house_access_code = models.CharField(max_length=50, blank=True, null=True)
	ssid = models.CharField(max_length=200, blank=True, null=True)
	ssid_password = models.CharField(max_length=200, blank=True, null=True)
	timezone = models.CharField(max_length=200)
	#stripe_secret_key = models.CharField(max_length=200)
	#stripe_public_key = models.CharField(max_length=200)
	#mailgun_api_key = models.CharField(max_length=200)
	#mailgun_domain = models.CharField(max_length=200)
	bank_account_number = models.IntegerField(max_length=200, blank=True, null=True, help_text="We use this to transfer money to you!")
	routing_number = models.IntegerField(max_length=200, blank=True, null=True, help_text="We use this to transfer money to you!")
	bank_name = models.CharField(max_length=200, blank=True, null=True, help_text="We use this to transfer money to you!")
	name_on_account = models.CharField(max_length=200, blank=True, null=True, help_text="We use this to transfer money to you!")
	email_subject_prefix = models.CharField(max_length=200, help_text="Your prefix will be wrapped in square brackets automatically.")
	#default_from_email = models.CharField(max_length=200)
	house_admins = models.ManyToManyField(User, related_name='house_admin', blank=True, null=True)
	residents = models.ManyToManyField(User, related_name='residences', blank=True, null=True)

	def __unicode__(self):
		return self.name

	def from_email(self):
		''' return a location-specific email in the standard format we use.'''
		return "stay@%s.mail.embassynetwork.com" % self.slug

class LocationNotUniqueException(Exception):
	pass

class LocationDoesNotExistException(Exception):
	pass

def get_location(location_slug):
	if location_slug:
		try:
			location = Location.objects.get(slug=location_slug)
		except:
			raise LocationDoesNotExistException("The requested location does not exist: %s" % location_slug)
	else:
		if Location.objects.count() == 1:
			location = Location.objects.get(id=1)
		else:
			raise LocationNotUniqueException("You did not specify a location and yet there is more than one location defined. Please specify a location.")
	return location

class RoomManager(models.Manager):
	def availability(self, start, end, location):
		# return a map of rooms to dates showing occupied and free beds,
		# between start and end dates, per location

		availability = {}
		the_day = start
		while the_day < end:
			# return only rooms available for bookings (currently just guest rooms)
			# XXX TODO add private rooms that have temporarily been added to the pool). 

			rooms_at_location = list(self.filter(primary_use="guest").filter(location=location))
			avail_today = {}
			for room, beds in [(room, room.beds) for room in rooms_at_location]:
				avail_today[room] = beds
			bookings_today = Reservation.objects.reserved_on_date(the_day, location)
			for booking in bookings_today:
				# if a room has been changed from a guest room
				# to some point in the past to a private room
				# now, then it won't be listed in the available
				# rooms_today, so we can't remove it. so just
				# skip over it, since it won't show up in
				# available rooms anyway. 
				try:
					if avail_today[booking.room] <= 0:
						avail_today[booking.room] = 0
					else:
						avail_today[booking.room] = avail_today[booking.room] -1 
				except:
					pass
			availability[the_day] = avail_today
			the_day = the_day + datetime.timedelta(1)

		# but it's actually easier to use the data if organizd by room...
		by_room = {}
		all_rooms_at_location = list(self.filter(location=location).filter(primary_use="guest"))
		for room in all_rooms_at_location:
			the_day = start
			by_room[room] = []
			while the_day < end:
				# use tuples to store the dates to ensure the proper ordering
				# is maintained. each tuple contains (date, num_beds_free)
				by_room[room].append((the_day, availability[the_day][room]))
				the_day = the_day + datetime.timedelta(1)
		return by_room


	def free(self, start, end, location):
		# return a list of room objects that are free the entire time between
		# start and end dates IMPT: make sure to COPY the list of initial
		# rooms - don't delete the real room objects :-O.
		room_list = list(self.filter(location=location).filter(primary_use="guest"))
		rooms = {}
		# make a list of all room and their associated # beds
		for r in room_list:
			rooms[r] = r.beds
		the_day = start
		while the_day < end:
			rooms_today = dict(rooms)
			bookings_today = Reservation.objects.reserved_on_date(the_day, location)
			for booking in bookings_today:
				# if a room has been changed from a guest room
				# to some point in the past to a private room
				# now, then it won't be listed in the available
				# rooms_today, so we can't remove it. so just
				# skip over it, since it won't show up in
				# available rooms anyway. 
				try:
					if rooms_today[booking.room] <= 0:
						rooms_today[booking.room] = 0
					else:
						rooms_today[booking.room] = rooms_today[booking.room] -1 
				except:
					pass
			the_day = the_day + datetime.timedelta(1)
			# be flexible if beds available is less than 0 in case an admin
			# wants to overbook a room for some reason, total beds availabile
			# changes, etc.
			full_today = [room for room in rooms_today.keys() if rooms_today[room] <=0 ]
			# can use sets here since the rooms are unique anyway
			room_list = list(set(room_list) - set(full_today)) 
		free_rooms = room_list
		return free_rooms

def room_img_upload_to(instance, filename):
	ext = filename.split('.')[-1]
	# rename file to random string
	filename = "%s.%s" % (uuid.uuid4(), ext.lower())

	upload_path = "data/rooms/%s/" % instance.name 
	upload_abs_path = os.path.join(settings.MEDIA_ROOT, upload_path)
	if not os.path.exists(upload_abs_path):
		os.makedirs(upload_abs_path)
	return os.path.join(upload_path, filename)

class Room(models.Model):

	GUEST = "guest"
	PRIVATE = "private"
	ROOM_USES = (
			(GUEST, "Guest"),
			(PRIVATE, "Private"),
			)
	name = models.CharField(max_length=200)
	location = models.ForeignKey(Location, related_name='rooms', null=True)
	default_rate = models.IntegerField()
	description = models.TextField(blank=True, null=True)
	primary_use = models.CharField(max_length=200, choices=ROOM_USES, default=PRIVATE, verbose_name='Indicate whether this room should be listed for guests to make reservations.')
	cancellation_policy = models.CharField(max_length=400, default="24 hours")
	shared = models.BooleanField(default=False, verbose_name="Is this room a hostel/shared accommodation?")
	beds = models.IntegerField()
	image = models.ImageField(upload_to=room_img_upload_to, blank=True, null=True)

	# manager
	objects = RoomManager()

	def __unicode__(self):
		return self.name

	def available_on(self, this_day, location):
		reservations_on_this_day = Reservation.objects.reserved_on_date(this_day, location=location)
		beds_left = self.beds
		for r in reservations_on_this_day:
			if r.room == self:
				beds_left -= 1
		if beds_left > 0:
			return True
		else:
			return False


class ReservationManager(models.Manager):

	def on_date(self, the_day, status, location):
		# return the reservations that intersect this day, of any status
		all_on_date = super(ReservationManager, self).get_query_set().filter(location=location).filter(arrive__lte = the_day).filter(depart__gt = the_day)
		return all_on_date.filter(status=status)

	def reserved_on_date(self, the_day, location):
		# return the approved or confirmed reservations that intersect this day
		approved_reservations = self.on_date(the_day, status= "approved", location=location)
		confirmed_reservations = self.on_date(the_day, status="confirmed", location=location)
		return (list(approved_reservations) + list(confirmed_reservations))

	def confirmed_on_date(self, the_day, location):	
		confirmed_reservations = self.on_date(the_day, status="confirmed", location=location)
		return list(confirmed_reservations)	

class TodayManager(models.Manager):
	def get_query_set(self, location):
		# return the reservations that intersect today
		today = datetime.date.today()
		return super(TodayManager, self).get_query_set().filter(location=location).filter(arrive__lte = today).filter(depart__gte = today)

	def confirmed(self, location):	
		# return the reservations that intersect today and are confirmed.
		today = datetime.date.today()
		return super(TodayManager, self).get_query_set().filter(location=location).filter(arrive__lte = today).filter(depart__gte = today).filter(status='confirmed')


class Reservation(models.Model):

	class ResActionError(Exception):
		def __init__(self, value):
			self.value = value
		def __str__(self):
			return repr(self.value)

	PENDING = 'pending'
	APPROVED = 'approved'
	CONFIRMED = 'confirmed'
	HOUSE_DECLINED = 'house declined'
	CANCELED = 'canceled'
	PAID = 'paid'
	USER_DECLINED = 'user declined'

	RESERVATION_STATUSES = (
			(PENDING, 'Pending'),
			(APPROVED, 'Approved'),
			(CONFIRMED, 'Confirmed'),
			(HOUSE_DECLINED, 'House Declined'),
			(USER_DECLINED, 'User Declined'),
			(CANCELED, 'Canceled'),
			)

	# hosted reservations - only exposed to house_admins. form fields are
	# rendered in the order they are declared in the model, so leave this
	# order as-is for these fields to appear at the top of the creation and
	# edit forms.
	hosted = models.BooleanField(default=False, help_text="Hosting a guest who doesn't have an account.")
	guest_name = models.CharField(max_length=200, help_text="Guest name if you are hosting.", blank=True, null=True)

	location = models.ForeignKey(Location, related_name='reservations', null=True)
	created = models.DateTimeField(auto_now_add=True)
	updated = models.DateTimeField(auto_now=True)
	status = models.CharField(max_length=200, choices=RESERVATION_STATUSES, default=PENDING, blank=True)
	user = models.ForeignKey(User, related_name='reservations')
	arrive = models.DateField(verbose_name='Arrival Date')
	depart = models.DateField(verbose_name='Departure Date')
	arrival_time = models.CharField(help_text='Optional, if known', max_length=200, blank=True, null=True)
	room = models.ForeignKey(Room, null=True)
	tags = models.CharField(max_length =200, help_text='What are 2 or 3 tags that characterize this trip?', blank=True, null=True)
	purpose = models.TextField(verbose_name='Tell us a bit about the reason for your trip/stay')
	comments = models.TextField(blank=True, null=True, verbose_name='Any additional comments. (Optional)')
	last_msg = models.DateTimeField(blank=True, null=True)

	# managers
	#objects = models.Manager()
	today = TodayManager() # approved and confirmed reservations that intersect today
	objects = ReservationManager()

	@models.permalink
	def get_absolute_url(self):
		return ('core.views.ReservationDetail', [str(self.location.slug), str(self.id)])

	def __unicode__(self):
		return "reservation %d" % self.id

	def total_nights(self):
		return (self.depart - self.arrive).days
	total_nights.short_description = "Nights"

	def mark_last_msg(self):
		self.last_msg = datetime.datetime.now()
		self.save()

	def set_tentative(self):
		self.status = Reservation.APPROVED
		self.save()

	def set_confirmed(self):
		self.status = Reservation.CONFIRMED
		self.save()

	def cancel(self):
		# cancel this reservation. make sure to update the associated reconcile
		# info. 
		self.status = Reservation.CANCELED
		self.save()
		reconcile = self.reconcile
		reconcile.status = Reconcile.INVALID
		reconcile.save()



class Reconcile(models.Model):
	''' The Reconcile object is automatically created when a reservation is confirmed.'''
	COMP = "comp"
	UNPAID = "unpaid"
	INVOICED = "invoiced"
	PAID = "paid"
	INVALID = "invalid"
	STATUSES = (
			(COMP, 'Comp'),
			(UNPAID, 'Unpaid'),
			(INVOICED, 'Invoiced'),
			(PAID, 'Paid'),
			(INVALID, "Invalid")
			)

	reservation = models.OneToOneField(Reservation)
	rate = models.IntegerField(null=True, blank=True, help_text="Uses the default rate unless otherwise specified.") 
	status = models.CharField(max_length=200, choices=STATUSES, default=UNPAID)
	automatic_invoice = models.BooleanField(default=False, help_text="If True, an invoice will be sent to the user automatically at the end of their stay.")
	payment_service = models.CharField(max_length=200, blank=True, null=True, help_text="e.g., Stripe, Paypal, Dwolla, etc. May be empty")
	payment_method = models.CharField(max_length=200, blank=True, null=True, help_text="e.g., Visa, cash, bank transfer")
	paid_amount = models.IntegerField(null=True, blank=True)
	transaction_id = models.CharField(max_length=200, null=True, blank=True)
	payment_date = models.DateField(blank=True, null=True)

	def __unicode__(self):
		return "reconcile reservation %d" % self.reservation.id

	def get_rate(self):
		if self.status == Reconcile.COMP:
			return 0
		else:
			return self.rate
	get_rate.short_description = 'Rate'

	def html_color_status(self):
		if self.status == Reconcile.PAID:
			color_code = "#5fbf00"
		elif self.status == Reconcile.UNPAID or self.status == Reconcile.INVOICED:
			color_code = "#bf0000"
		elif self.status == Reconcile.COMP:
			color_code = "#ffc000"
		else:
			color_code = "#000000"
		return '<span style="color: %s;">%s</span>' % (color_code, self.status)
	html_color_status.allow_tags = True

	def default_rate(self):
		# default_rate always returns the default rate regardless of comps or
		# custom rates.
		return self.reservation.room.default_rate

	def total_value(self):
		# value of the reservation, regardless of what has been paid
		# get_rate checks for comps and custom rates. 
		return self.reservation.total_nights()*self.get_rate()

	def total_owed(self):
		# in case some amoutn has been paid already
		if not self.paid_amount:
			return self.reservation.total_nights()*self.get_rate()
		else:
			return self.reservation.total_nights()*self.get_rate() - self.paid_amount


	def total_owed_in_cents(self):
		return self.total_owed()*100

	def send_invoice(self):
		''' trigger a reminder email to the guest about payment.''' 
		if self.reservation.hosted:
			# XXX TODO make this a proper error which is viewable in the admin form.
			print "hosted reservation invoices not supported"
			return
		if not self.status == Reconcile.UNPAID:
			# XXX TODO eventually send an email for COMPs too, but a
			# different once, with thanks/asking for feedback.
			return

		plaintext = get_template('emails/invoice.txt')
		htmltext = get_template('emails/invoice.html')
		c = Context({
			'first_name': self.reservation.user.first_name, 
			'res_id': self.reservation.id,
			'today': datetime.datetime.today(), 
			'arrive': self.reservation.arrive, 
			'depart': self.reservation.depart, 
			'room': self.reservation.room.name, 
			'num_nights': self.reservation.total_nights(), 
			'rate': self.get_rate(), 
			'total': self.total_owed,
			}) 

		subject = "[%s] Thanks for Staying with us!" % self.reservation.location.email_subject_prefix 
		recipient = [self.reservation.user.email,]
		text_content = plaintext.render(c)
		html_content = htmltext.render(c)
		send_from_location_address(subject, text_content, html_context, recipient, self.reservation.location)
		self.status = Reconcile.INVOICED
		self.save()

	def charge_card(self):
		# stripe will raise a stripe.CardError if the charge fails. this
		# function purposefully does not handle that error so the calling
		# function can decide what to do. 
		domain = 'http://' + Site.objects.get_current().domain
		descr = "%s %s from %s - %s. Details: %s." % (self.reservation.user.first_name, 
				self.reservation.user.last_name, str(self.reservation.arrive), 
				str(self.reservation.depart), domain + self.reservation.get_absolute_url())

		amt_owed = self.total_owed_in_cents()
		stripe.api_key = settings.STRIPE_SECRET_KEY
		charge = stripe.Charge.create(
				amount=amt_owed,
				currency="usd",
				customer = self.reservation.user.profile.customer_id,
				description=descr
				)

		# store the charge details
		self.status = Reconcile.PAID
		self.payment_service = "Stripe"
		self.payment_method = charge.card.type
		self.paid_amount = (charge.amount/100)
		self.transaction_id = charge.id
		self.payment_date = datetime.datetime.now()
		self.save()


Reservation.reconcile = property(lambda r: Reconcile.objects.get_or_create(reservation=r)[0])

@receiver(pre_save, sender=Reconcile)
def set_rate(sender, instance, **kwargs):
	try:
		rec = Reconcile.objects.get(pk=instance.pk)
	except Reconcile.DoesNotExist:
		# if the reconcile object does not exist yet, then it's new. set the
		# rate from the default rate for this room. 
		instance.rate = instance.reservation.room.default_rate
	return



def profile_img_upload_to(instance, filename):
	ext = filename.split('.')[-1]
	# rename file to random string
	filename = "%s.%s" % (uuid.uuid4(), ext.lower())

	upload_path = "data/avatars/%s/" % instance.user.username
	upload_abs_path = os.path.join(settings.MEDIA_ROOT, upload_path)
	if not os.path.exists(upload_abs_path):
		os.makedirs(upload_abs_path)
	return os.path.join(upload_path, filename)

def get_default_profile_img():
	path = os.path.join(settings.MEDIA_ROOT, "data/avatars/default.jpg")
	return file(path)

class UserProfile(models.Model):
	IMG_SIZE = (300,300)
	IMG_THUMB_SIZE = (150,150)

	# User model fields: username, first_name, last_name, email, 
	# password, is_staff, is_active, is_superuser, last_login, date_joined,
	user = models.OneToOneField(User)
	updated = models.DateTimeField(auto_now=True)
	image = models.ImageField(upload_to=profile_img_upload_to, help_text="Image should have square dimensions.")
	image_thumb = models.ImageField(upload_to="data/avatars/%Y/%m/%d/", blank=True, null=True)
	bio = models.TextField("About you", blank=True, null=True)
	links = models.TextField(help_text="Comma-separated", blank=True, null=True)

	projects = models.TextField(verbose_name='Current Projects', help_text='Describe one or more projects you are currently working on')
	sharing = models.TextField(help_text="Is there anything you'd be interested in learning or sharing during your stay?")
	discussion = models.TextField(help_text="We like discussing thorny issues with each other. What's a question that's been on your mind lately that you don't know the answer to?")
	referral = models.CharField(max_length=200, verbose_name='How did you hear about us? (Give a name if possible!)')
	# currently used to store the stripe customer id but could be used for
	# other payment platforms in the future
	customer_id = models.CharField(max_length=200, blank=True, null=True)

	def __unicode__(self):
		return (self.user.__unicode__())

User.profile = property(lambda u: UserProfile.objects.get_or_create(user=u)[0])

User._meta.ordering = ['username']

@receiver(pre_save, sender=UserProfile)
def size_images(sender, instance, **kwargs):
	print 'in size images'
	print instance.image
	try:
		obj = UserProfile.objects.get(pk=instance.pk)
		print obj.image
	except UserProfile.DoesNotExist:
		# if the reservation does not exist yet, then it's new. 
		obj = None

	# if this is the default avatar, reuse it for the thumbnail (lazy, but only
	# for backwards compatibility for those who created accounts before images
	# were required)
	if instance.image.name == "data/avatars/default.jpg":
		instance.image_thumb = "data/avatars/default.thumb.jpg"

	elif instance.image and (obj == None or obj.image != instance.image or obj.image_thumb == None):
		im = Image.open(instance.image)

		img_upload_path_rel = profile_img_upload_to(instance, instance.image.name)
		main_img_full_path = os.path.join(settings.MEDIA_ROOT, img_upload_path_rel)
		# resize returns a copy. resize() forces the dimensions of the image
		# to match SIZE specified, squeezing the image if necessary along one
		# dimension.
		main_img = im.resize(UserProfile.IMG_SIZE, Image.ANTIALIAS)
		main_img.save(main_img_full_path)
		# the image field is a link to the path where the image is stored
		instance.image = img_upload_path_rel
		# now resize this to generate the smaller thumbnail
		thumb_img = im.resize(UserProfile.IMG_THUMB_SIZE, Image.ANTIALIAS)
		thumb_full_path = os.path.splitext(main_img_full_path)[0] + ".thumb" + os.path.splitext(main_img_full_path)[1]
		thumb_img.save(thumb_full_path)
		# the ImageFileField needs the path info relative to the media
		# directory
		# XXX Q: does this save the file twice? once by PIL and another time
		# reading it in and saving it to the same place when the model saves?
		thumb_rel_path = os.path.join(os.path.split(img_upload_path_rel)[0], os.path.basename(thumb_full_path))
		instance.image_thumb = thumb_rel_path

		# now delete any old images
		if obj and obj.image and obj.image.name != "data/avatars/default.jpg":
			default_storage.delete(obj.image.path)

		if obj and obj.image_thumb and obj.image_thumb.name != "data/avatars/default.thumb.jpg":
			default_storage.delete(obj.image_thumb.path)


class EmailTemplate(models.Model):
	''' Templates for the typical emails sent by the system. The from-address
	is usually set by DEFAULT_FROM_ADDRESS in settings, and the recipients are
	determined by the action and reservation in question. ''' 

	SUBJECT_PREFIX = settings.EMAIL_SUBJECT_PREFIX 
	FROM_ADDRESS = settings.DEFAULT_FROM_EMAIL

	body = models.TextField(verbose_name="The body of the email")
	subject = models.CharField(max_length=200, verbose_name="Default Subject Line")
	name = models.CharField(max_length=200, verbose_name="Template Name")
	creator = models.ForeignKey(User)
	shared = models.BooleanField(default=False)

	def __unicode__(self):
		return self.name





