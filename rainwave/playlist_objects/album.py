import os
import time
import math

from libs import db
from libs import log
from libs import config
from libs import cache
from rainwave import rating

from rainwave.playlist_objects.metadata import MetadataNotFoundError
from rainwave.playlist_objects.metadata import AssociatedMetadata
from rainwave.playlist_objects.metadata import make_searchable_string
from rainwave.playlist_objects import cooldown

# REFACTORING TODO:
# update_album_ratings / get_album_rating must be updated
# do a project wide search for r4_song_sid
# fix API help page so I don't have to explain what it is when someone visits it
# finish the migrate script
# add more indexes to the ratings tables? maybe some sid indexes too...
	# - c.create_idx("r4_song_ratings", "user_id", "song_id")
	# - c.create_idx("r4_album_ratings", "user_id", "album_id") ????? already created?!
	# - c.create_idx("r4_album_ratings", "sid")

# CHECK FOR STASHED / LOST WORK ON WORKSTATION
# cache.py
# rating.py

updated_album_ids = {}

def clear_updated_albums(sid):
	global updated_album_ids
	updated_album_ids[sid] = {}

def get_updated_albums_dict(sid):
	global updated_album_ids
	if not sid in updated_album_ids:
		return []

	previous_newest_album = cache.get_station(sid, "newest_album")
	if not previous_newest_album:
		cache.set_station(sid, "newest_album", time.time())
	else:
		newest_albums = db.c.fetch_list("SELECT album_id FROM r4_albums JOIN r4_album_sid USING (album_id) WHERE sid = %s AND album_added_on > %s", (sid, previous_newest_album))
		for album_id in newest_albums:
			updated_album_ids[sid][album_id] = True
		cache.set_station(sid, "newest_album", time.time())
	album_diff = []
	for album_id in updated_album_ids[sid]:
		album = Album.load_from_id_sid(album_id, sid)
		album.solve_cool_lowest(sid)
		tmp = album.to_dict()
		# Remove user-related stuff since this gets stuffed straight down the pipe
		tmp.pop('rating_user', None)
		tmp.pop('fave', None)
		album_diff.append(tmp)
	return album_diff

def warm_cooled_albums(sid):
	global updated_album_ids
	album_list = db.c.fetch_list("SELECT album_id FROM r4_album_sid WHERE sid = %s AND album_cool_lowest <= %s AND album_cool = TRUE", (sid, time.time()))
	for album_id in album_list:
		updated_album_ids[sid][album_id] = True
	db.c.update("UPDATE r4_album_sid SET album_cool = FALSE WHERE sid = %s AND album_cool_lowest <= %s AND album_cool = TRUE", (sid, time.time()))

class Album(AssociatedMetadata):
	select_by_name_query = "SELECT r4_albums.* FROM r4_albums WHERE album_name = %s"
	select_by_id_query = "SELECT r4_albums.* FROM r4_albums WHERE album_id = %s"
	select_by_song_id_query = "SELECT r4_albums.* FROM r4_songs JOIN r4_albums USING (album_id) WHERE song_id = %s"
	has_song_id_query = "SELECT COUNT(song_id) FROM r4_songs WHERE song_id = %s AND album_id = %s"
	# This is a hack, but,umm..... yeah.  It'll do. :P  reconcile_sids handles these duties.
	check_self_size_query = "SELECT 1, %s"
	# delete_self_query will never run, but just in case
	delete_self_query = "SELECT 1, %s"

	@classmethod
	def load_from_id_sid(cls, album_id, sid):
		row = db.c.fetch_row("SELECT r4_albums.*, album_cool, album_cool_lowest, album_cool_multiply, album_cool_override FROM r4_album_sid JOIN r4_albums USING (album_id) WHERE r4_album_sid.album_id = %s AND r4_album_sid.sid = %s", (album_id, sid))
		if not row:
			raise MetadataNotFoundError("%s ID %s for sid %s could not be found." % (cls.__name__, album_id, sid))
		instance = cls()
		instance._assign_from_dict(row, sid)
		instance.data['sids'] = [ sid ]
		return instance

	@classmethod
	def load_from_id_with_songs(cls, album_id, sid, user = None):
		row = db.c.fetch_row("SELECT * FROM r4_albums WHERE album_id = %s", (album_id,))
		instance = cls()
		instance._assign_from_dict(row, sid)
		instance.data['sids'] = [ sid ]
		user_id = None if not user else user.id
		requestable = True if user else False
		instance.data['songs'] = db.c.fetch_all(
			"SELECT r4_song_sid.song_id AS id, song_length AS length, song_origin_sid AS origin_sid, song_title AS title, "
				"song_url AS url, song_link_text AS link_text, song_rating AS rating, song_cool_multiply AS cool_multiply, "
				"song_cool_override AS cool_override, %s AS requestable, song_cool AS cool, song_cool_end AS cool_end, "
				"song_request_only AS request_only, song_artist_parseable AS artist_parseable, "
				"COALESCE(song_rating_user, 0) AS rating_user, COALESCE(song_fave, FALSE) AS fave "
			"FROM r4_song_sid "
				"JOIN r4_songs USING (song_id) "
				"LEFT JOIN r4_song_ratings ON (r4_song_sid.song_id = r4_song_ratings.song_id AND user_id = %s) "
			"WHERE r4_song_sid.song_exists = TRUE AND r4_songs.song_verified = TRUE AND r4_songs.album_id = %s AND r4_song_sid.sid = %s "
			"ORDER BY song_title",
			(requestable, user_id, instance.id, sid))
		return instance

	@classmethod
	def get_art_url(self, album_id, sid = None):
		if not config.get("album_art_file_path"):
			return ""
		elif sid and os.path.isfile(os.path.join(config.get("album_art_file_path"), "%s_%s.jpg" % (sid, album_id))):
			return "%s/%s_%s" % (config.get("album_art_url_path"), sid, album_id)
		elif os.path.isfile(os.path.join(config.get("album_art_file_path"), "%s.jpg" % album_id)):
			return "%s/%s" % (config.get("album_art_url_path"), album_id)
		return ""

	def __init__(self):
		super(Album, self).__init__()
		self.data['sids'] = []

	def _insert_into_db(self):
		global updated_album_ids

		self.id = db.c.get_next_id("r4_albums", "album_id")
		success = db.c.update("INSERT INTO r4_albums (album_id, album_name, album_name_searchable) VALUES (%s, %s, %s)", (self.id, self.data['name'], make_searchable_string(self.data['name'])))
		for sid in self.data['sids']:
			updated_album_ids[sid][self.id] = True
		return success

	def _update_db(self):
		global updated_album_ids

		success = db.c.update(
			"UPDATE r4_albums "
			"SET album_name = %s, album_name_searchable = %s, album_rating = %s "
			"WHERE album_id = %s",
			(self.data['name'], make_searchable_string(self.data['name']), self.data['rating'], self.id))
		for sid in self.data['sids']:
			updated_album_ids[sid][self.id] = True
		return success

	def _assign_from_dict(self, d, sid = None):
		self.id = d['album_id']
		self.data['name'] = d['album_name']
		self.data['rating'] = d['album_rating']
		self.data['rating_count'] = d['album_rating_count']
		self.data['added_on'] = d['album_added_on']
		self._dict_check_assign(d, "album_added_on")
		self._dict_check_assign(d, "album_cool_multiply", 1)
		self._dict_check_assign(d, "album_cool_override")
		self._dict_check_assign(d, "album_cool_lowest", 0)
		self._dict_check_assign(d, "album_played_last", 0)
		self._dict_check_assign(d, "album_fave_count", 0)
		self._dict_check_assign(d, "album_vote_count", 0)
		self._dict_check_assign(d, "album_request_count", 0)
		self._dict_check_assign(d, "album_cool", False)
		self._dict_check_assign(d, "album_name_searchable", self.data['name'])
		if d.has_key('sid'):
			self.data['sid'] = d['sid']
		if d.has_key('album_is_tag'):
			self.is_tag = d['album_is_tag']
		self.data['art'] = Album.get_art_url(self.id, sid)

	def _dict_check_assign(self, d, key, default = None, new_key = None):
		if not new_key and key.find("album_") == 0:
			new_key = key[6:]
		if d.has_key(key):
			self.data[new_key] = d[key]
		else:
			self.data[new_key] = default

	def get_num_songs(self, sid):
		return db.c.fetch_var("SELECT COUNT(song_id) FROM r4_song_sid JOIN r4_songs USING (song_id) WHERE album_id = %s AND sid = %s", (self.id, sid))

	def associate_song_id(self, song_id, sids, is_tag = None):
		# Deprecated
		pass

	def disassociate_song_id(self, song_id):
		# Deprecated
		pass

	def reconcile_sids(self, album_id = None):
		# Deprecated
		pass

	def start_cooldown(self, sid, cool_time = False):
		if cool_time:
			pass
		elif self.data['cool_override']:
			cool_time = self.data['cool_override']
		else:
			cool_rating = self.data['rating']
			if not cool_rating or cool_rating == 0:
				cool_rating = 3
			# AlbumCD = minAlbumCD + ((maxAlbumR - albumR)/(maxAlbumR - minAlbumR)*(maxAlbumCD - minAlbumCD))
			# old: auto_cool = cooldown.cooldown_config[sid]['min_album_cool'] + (((4 - (cool_rating - 1)) / 4.0) * (cooldown.cooldown_config[sid]['max_album_cool'] - cooldown.cooldown_config[sid]['min_album_cool']))
			auto_cool = cooldown.cooldown_config[sid]['min_album_cool'] + (((5 - cool_rating) / 4.0) * (cooldown.cooldown_config[sid]['max_album_cool'] - cooldown.cooldown_config[sid]['min_album_cool']))
			album_num_songs = self.get_num_songs()
			log.debug("cooldown", "min_album_cool: %s .. max_album_cool: %s .. auto_cool: %s .. album_num_songs: %s .. rating: %s" % (cooldown.cooldown_config[sid]['min_album_cool'], cooldown.cooldown_config[sid]['max_album_cool'], auto_cool, album_num_songs, cool_rating))
			cool_size_multiplier = config.get_station(sid, "cooldown_size_min_multiplier") + (config.get_station(sid, "cooldown_size_max_multiplier") - config.get_station(sid, "cooldown_size_min_multiplier")) / (1 + math.pow(2.7183, (config.get_station(sid, "cooldown_size_slope") * (album_num_songs - config.get_station(sid, "cooldown_size_slope_start")))) / 2)
			cool_age_multiplier = cooldown.get_age_cooldown_multiplier(self.data['added_on'])
			cool_time = int(auto_cool * cool_size_multiplier * cool_age_multiplier * self.data['cool_multiply'])
			log.debug("cooldown", "auto_cool: %s .. cool_size_multiplier: %s .. cool_age_multiplier: %s .. cool_multiply: %s .. cool_time: %s" %
					  (auto_cool, cool_size_multiplier, cool_age_multiplier, self.data['cool_multiply'], cool_time))
		updated_album_ids[sid][self.id] = True
		log.debug("cooldown", "Album ID %s Station ID %s cool_time period: %s" % (self.id, sid, cool_time))
		return self._start_cooldown_db(sid, cool_time)

	def _start_cooldown_db(self, sid, cool_time):
		cool_end = int(cool_time + time.time())
		if db.c.allows_join_on_update:
			db.c.update("UPDATE r4_song_sid "
							"SET song_cool = TRUE, song_cool_end = %s "
							"FROM r4_songs "
							"WHERE r4_song_sid.song_id = r4_songs.song_id AND album_id = %s AND sid = %s AND song_cool_end < %s",
						(cool_end, self.id, sid, cool_end))
		else:
			songs = db.c.fetch_list("SELECT song_id FROM r4_song_sid JOIN r4_songs USING (song_id) WHERE album_id = %s AND sid = %s AND song_exists = TRUE AND song_cool_end < %s", (self.id, sid, cool_end))
			for song_id in songs:
				db.c.update("UPDATE r4_song_sid SET song_cool = TRUE, song_cool_end = %s WHERE song_id = %s AND song_cool_end < %s AND sid = %s", (cool_end, song_id, cool_end, sid))

	def solve_cool_lowest(self, sid):
		self.data['cool_lowest'] = db.c.fetch_var("SELECT MIN(song_cool_end) FROM r4_song_sid JOIN r4_songs USING (song_id) WHERE album_id = %s AND sid = %s AND song_exists = TRUE", (self.id, sid))
		if self.data['cool_lowest'] > time.time():
			self.data['cool'] = True
		else:
			self.data['cool'] = False
		db.c.update("UPDATE r4_album_sid SET album_cool_lowest = %s, album_cool = %s WHERE album_id = %s AND sid = %s", (self.data['cool_lowest'], self.data['cool'], self.id, sid))
		return self.data['cool_lowest']

	def update_rating(self):
		dislikes = db.c.fetch_var("SELECT COUNT(*) FROM r4_album_ratings JOIN phpbb_users USING (user_id) WHERE radio_inactive = FALSE AND album_id = %s AND album_rating_user < 3 GROUP BY album_id", (self.id,))
		if not dislikes:
			dislikes = 0
		neutrals = db.c.fetch_var("SELECT COUNT(*) FROM r4_album_ratings JOIN phpbb_users USING (user_id) WHERE radio_inactive = FALSE AND album_id = %s AND album_rating_user >= 3 AND album_rating_user < 3.5 GROUP BY album_id", (self.id,))
		if not neutrals:
			neutrals = 0
		neutralplus = db.c.fetch_var("SELECT COUNT(*) FROM r4_album_ratings JOIN phpbb_users USING (user_id) WHERE radio_inactive = FALSE AND album_id = %s AND album_rating_user >= 3.5 AND album_rating_user < 4 GROUP BY album_id", (self.id,))
		if not neutralplus:
			neutralplus = 0
		likes = db.c.fetch_var("SELECT COUNT(*) FROM r4_album_ratings JOIN phpbb_users USING (user_id) WHERE radio_inactive = FALSE AND album_id = %s AND album_rating_user >= 4 GROUP BY album_id", (self.id,))
		if not likes:
			likes = 0
		rating_count = dislikes + neutrals + neutralplus + likes
		if rating_count > config.get("rating_threshold_for_calc"):
			self.rating = round(((((likes + (neutrals * 0.5) + (neutralplus * 0.75)) / (likes + dislikes + neutrals + neutralplus) * 4.0)) + 1), 1)
			db.c.update("UPDATE r4_albums SET album_rating = %s, album_rating_count = %s WHERE album_id = %s", (self.rating, rating_count, self.id))

	def update_last_played(self, sid):
		return db.c.update("UPDATE r4_album_sid SET album_played_last = %s WHERE album_id = %s AND sid = %s", (time.time(), self.id, sid))

	def update_vote_total(self, sid):
		vote_total = db.c.fetch_var("SELECT SUM(song_vote_count) FROM r4_song_sid JOIN r4_songs USING (song_id) WHERE album_id = %s AND song_exists = TRUE", (self.id,))
		return db.c.update("UPDATE r4_album_sid SET album_vote_count = %s WHERE album_id = %s AND sid = %s", (vote_total, self.id, sid))

	def get_all_ratings(self):
		table = db.c.fetch_all("SELECT album_rating_user, album_fave, user_id FROM r4_album_ratings JOIN phpbb_users USING (user_id) WHERE radio_inactive = FALSE AND album_id = %s", (self.id,))
		all_ratings = {}
		for row in table:
			all_ratings[row['user_id']] = { "rating_user": row['album_rating_user'], "fave": row['album_fave'] }
		return all_ratings

	def update_all_user_ratings(self):
		for sid in config.station_ids:
			num_songs = self.get_num_songs(sid)
			db.c.update(
				"WITH "
					"faves AS ( "
						"DELETE FROM r4_album_ratings WHERE album_id = %s RETURNING * "
					"), "
					"ratings AS ( "
						"SELECT album_id, sid, FALSE AS album_fave, user_id, ROUND(CAST(AVG(song_rating_user) AS NUMERIC), 1) AS album_rating_user, COUNT(song_rating_user) AS song_rating_user_count "
						"FROM ("
							"SELECT song_id, sid, album_id FROM r4_song_sid JOIN r4_songs WHERE album_id = %s AND sid = %s"
						") AS r4_song_sid LEFT JOIN r4_song_ratings USING (song_id) "
						"GROUP BY album_id, user_id "
					") "
				"INSERT INTO r4_album_ratings (sid, album_id, user_id, album_fave, album_rating_user, album_rating_complete) "
				"SELECT sid, album_id, user_id, BOOL_OR(album_fave) AS album_fave, NULLIF(MAX(album_rating_user), 0) AS album_rating_user, CASE WHEN MAX(song_rating_user_count) >= %s THEN TRUE ELSE FALSE END AS album_rating_complete "
				"FROM (SELECT * FROM (SELECT album_id, album_fave, user_id, 0 AS album_rating_user, 0 AS song_rating_user_count FROM faves) AS faves UNION ALL SELECT * FROM ratings) AS fused "
				"GROUP BY sid, album_id, user_id "
				"HAVING BOOL_OR(album_fave) = TRUE OR MAX(album_rating_user) IS NOT NULL ",
				(self.id, sid, self.id, num_songs))

	def reset_user_completed_flags(self, sid):
		db.c.update("UPDATE r4_album_ratings SET album_rating_complete = FALSE WHERE album_id = %s AND sid = %s", (self.id, sid))

	def _start_election_block_db(self, sid, num_elections):
		if db.c.allows_join_on_update:
			# refer to song.set_election_block for base SQL
			db.c.update("UPDATE r4_song_sid "
							"SET song_elec_blocked = TRUE, song_elec_blocked_by = %s, song_elec_blocked_num = %s "
							"FROM r4_songs "
							"WHERE r4_song_sid.song_id = r4_songs.song_id AND album_id = %s AND sid = %s AND song_elec_blocked_num <= %s",
						('album', num_elections, self.id, sid, num_elections))
		# Disabled for SQLite to avoid circular module importing
		# else:
		# 	table = db.c.fetch_all("SELECT song_id FROM r4_song_sid JOIN r4_songs USING (song_id) WHERE album_id = %s AND sid = %s", (self.id, sid))
		# 	for row in table:
		# 		song = Song()
		# 		song.id = row['song_id']
		# 		song.set_election_block(sid, 'album', num_elections)

	def load_extra_detail(self, sid):
		self.data['rating_rank'] = 1 + db.c.fetch_var("SELECT COUNT(album_id) FROM r4_album_sid WHERE album_rating > %s AND sid = %s", (self.data['rating'], sid))
		self.data['request_rank'] = 1+ db.c.fetch_var("SELECT COUNT(album_id) FROM r4_album_sid WHERE album_request_count > %s AND sid = %s", (self.data['request_count'], sid))

		self.data['genres'] = db.c.fetch_all(
			"SELECT DISTINCT group_id AS id, group_name AS name "
			"FROM r4_song_sid JOIN r4_songs USING (song_id) r4_song_group USING (song_id) JOIN r4_groups USING (group_id) "
			"WHERE album_id = %s",
			(self.id,))

		self.data['rating_histogram'] = {}
		histo = db.c.fetch_all("SELECT "
							   "ROUND(((album_rating_user * 10) - (CAST(album_rating_user * 10 AS SMALLINT) %% 5))) / 10 AS rating_rnd, "
							   "COUNT(album_rating_user) AS rating_count "
							   "FROM r4_album_ratings JOIN phpbb_users USING (user_id) "
							   "WHERE radio_inactive = FALSE AND album_id = %s AND sid = %s "
							   "GROUP BY rating_rnd "
							   "ORDER BY rating_rnd",
							   (self.id, sid))
		for point in histo:
			self.data['rating_histogram'][str(point['rating_rnd'])] = point['rating_count']

	def update_request_count(self, sid):
		count = db.c.fetch_var("SELECT COUNT(*) FROM r4_songs JOIN r4_request_history USING (song_id) WHERE album_id = %s AND sid = %s USING (song_id)", (self.id, sid))
		return db.c.update("UPDATE r4_album_sid SET album_request_count = %s WHERE album_id = %s AND sid = %s", (count, self.id, sid))

	def update_fave_count(self, sid):
		count = db.c.fetch_var("SELECT COUNT(*) FROM r4_album_ratings WHERE album_fave = TRUE AND album_id = %s AND sid = %s", (self.id, sid))
		return db.c.update("UPDATE r4_album_sid SET album_fave_count = %s WHERE album_id = %s AND sid = %s", (count, self.id, sid))

	def update_vote_count(self, sid):
		count = db.c.fetch_var("SELECT COUNT(song_id) FROM r4_song_sid JOIN r4_songs USING (song_id) JOIN r4_vote_history USING (song_id) album_id = %s AND sid = %s A JOIN r4_vote_history USING (song_id)", (self.id,))
		return db.c.update("UPDATE r4_album_sid SET album_vote_count = %s WHERE album_id = %s AND sid = %s", (count, self.id, sid))

	def to_dict(self, user = None):
		d = super(Album, self).to_dict(user)
		if user:
			self.data.update(rating.get_album_rating(self.id, user.id))
		else:
			d['rating_user'] = None
			d['fave'] = None
		return d