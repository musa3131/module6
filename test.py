import config
import telebot
from telebot.apihelper import ApiException
import re
import codecs
import random
from os.path import getsize
from pymongo import MongoClient, ReturnDocument
from enum import Enum, auto
from time import time
from uuid import uuid4
from datetime import datetime
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton


def delete_overdue_req():
    while True:
        delete_result = database.requests.delete_many({'time': {'$lte': time()}})
        deleted_count = delete_result.deleted_count
        if deleted_count > 0:
            logger.info(f'Удалено просроченных заявок: {deleted_count}')


def Check_over(game):
    try:
        alive_players = [p for p in game['players'] if p['alive']]
        mafia = sum(p['role'] in ('don', 'mafia') for p in alive_players)
        return 1 if not mafia else 2 if mafia >= len(alive_players) - mafia else 0
    except KeyError:
        return 0

# p = player
# g = game
def stage_cycle():
    while True:
        modify = database.games.find({'game': 'mafia', 'next_stage_time': {'$lte': time()}})
        for g in modify:
            g_state = Check_over(g)
            if g_state:
                role = role_titles['peace' if g_state == 1 else 'mafia']
                for p in g['players']:
                    p_role = p['role'] if p['role'] != 'don' else 'mafia'
                    inc_dict = {'total': 1, f'{p_role}.total': 1}
                    if (
                            (g_state == 1 and p_role != 'mafia') or
                            (g_state == 2 and p_role == 'mafia')
                    ):
                        inc_dict['win'] = 1
                        inc_dict[f'{p_role}.win'] = 1
                    database.stats.update_one(
                        {'id': p['id'], 'chat': g['chat']},
                        {'$set': {'name': p['full_name']}, '$inc': inc_dict},
                        upsert=True
                    )
                stop_g(g, reason=f'Победили игроки команды "{role}"!')
                continue

            g = go_to_next_stage(g)


def croco_cycle():
    while True:
        curtime = time()
        g = list(database.games.find({'game': 'croco', 'time': {'$lte': curtime}}))
        for game in g:
            if game['stage'] == 0:
                database.games.update_one({'_id': game['_id']}, {'$set': {'stage': 1, 'time': curtime + 60}})
                bot.try_to_send_message(game['chat'], f'{game["name"].capitalize()}, до конца игры осталась минута!')
            else:
                database.games.delete_one({'_id': game['_id']})
                bot.try_to_send_message(game['chat'],
                                        f'Игра окончена! {game["name"].capitalize()} проигрывает, загаданное слово было {game["word"]}.')
                database.stats.update_one(
                    {'id': game['player'], 'chat': game['chat']},
                    {'$set': {'name': game['full_name']}, '$inc': {'croco.total': 1}},
                    upsert=True
                )


def start_thread(name=None, target=None, *args, daemon=True, **kwargs):
    thread = Thread(*args, name=name, target=target, daemon=daemon, **kwargs)
    logger.debug(f'Запускаю процесс <{thread.name}>')
    thread.start()


def run_app():
    app = flask.Flask(__name__)

    @app.route('/' + config.TOKEN, methods=['POST'])
    def webhook():
        if flask.request.headers.get('content-type') == 'application/json':
            json_string = flask.request.get_data().decode('utf-8')
            update = Update.de_json(json_string)
            log_update(update)
            bot.process_new_updates([update])
            return ''
        else:
            flask.abort(403)

    app.run(
        host=config.SERVER_IP,
        port=config.SERVER_PORT,
        ssl_context=(config.SSL_CERT, config.SSL_PRIV),
        debug=False
    )


def main():
    start_thread('Stage Cycle', stage_cycle)
    start_thread('Removing Requests', delete_overdue_req)
    start_thread('Crocodile Cycle', croco_cycle)

    if config.SET_WEBHOOK:
        url = f'https://{config.SERVER_IP}:{config.SERVER_PORT}/'
        logger.debug(f'Запускаю приложение по адресу {url}')
        run_app()
        bot.remove_webhook()
        bot.set_webhook(url=url + config.TOKEN)
    else:
        bot.polling()


def group_only(message):
    return message.chat.type in ('group', 'supergroup')


class MafiaHostBot(TeleBot):
    def try_to_send_message(self, *args, **kwargs):
        try:
            self.send_message(*args, **kwargs)
        except ApiException:
            logger.error('Ошибка API при отправке сообщения', exc_info=True)

    def _game_handler(self, handler):
        def decorator(message, *args, **kwargs):
            game = database.games.find_one({'chat': message.chat.id})
            if game and game['game'] == 'mafia':
                try:
                    player = next(p for p in game['players'] if p['id'] == message.from_user.id)
                except StopIteration:
                    delete = config.DELETE_FROM_EVERYONE and game['stage'] not in (0, -4)
                else:
                    if game['stage'] in (2, 7):
                        victim = game.get('victim')
                        delete = not player.get('alive', True) if victim is None else victim != message.from_user.id
                    else:
                        delete = not player.get('alive', True) or game['stage'] not in (0, -4)
                if delete:
                    self.safely_delete_message(chat_id=message.chat.id, message_id=message.message_id)
                    return

            return handler(message, game, *args, **kwargs)

        return decorator

    def group_message_handler(self, *, func=None, **kwargs):
        def decorator(handler):
            if func is None:
                conjuction = group_only
            else:
                conjuction = lambda message: group_only(message) and func(message)

            new_handler = self._game_handler(handler)
            handler_dict = self._build_handler_dict(new_handler, func=conjuction, **kwargs)
            self.add_message_handler(handler_dict)
            return new_handler

        return decorator

    def safely_delete_message(self, *args, **kwargs):
        try:
            self.delete_message(*args, **kwargs)
        except ApiException:
            logger.debug('Ошибка API при удалении сообщения', exc_info=True)


def get_word():
    with codecs.open(config.WORD_BASE, 'r', encoding='cp1251') as base:
        offset = random.randrange(BASE_SIZE)
        base.seek(offset)
        base.readline()
        word = base.readline()
    return word


def croco_suggestion(suggestion, game, user, message_id):
    if not re.search(r'\b{}\b'.format(game['word']), suggestion):
        return
    increments = {'croco.total': 1}
    if user['id'] == game['player']:
        increments['croco.cheat'] = 1
        answer = 'Игра окончена! Нельзя самому называть слово!'
    else:
        increments['croco.win'] = 1
        database.stats.update_one(
            {'id': user['id'], 'chat': game['chat']},
            {'$set': {'name': user['full_name']}, '$inc': {'croco.guesses': 1}},
            upsert=True
        )
        answer = 'Игра окончена! Это верное слово!'
    bot.send_message(game['chat'], answer, reply_to_message_id=message_id)
    database.games.delete_one({'_id': game['_id']})
    database.stats.update_one(
        {'id': game['player'], 'chat': game['chat']},
        {'$set': {'name': game['full_name']}, '$inc': increments},
        upsert=True
    )


def get_new_id(collection):
    counter = database.counter.find_one_and_update(
        {"_id": collection},
        {"$inc": {"next": 1}},
        return_document=ReturnDocument.AFTER,
        upsert=True
    )

    return counter["next"]


def get_stats(game):
    stats = {int(id): {'name': name, 'right': 0, 'wrong': 0} for id, name in game['names'].items()}
    for key in ('right', 'wrong'):
        for user_id in game[key].values():
            stats[user_id][key] += 1
    return stats


def set_gallows(game, result, word, stats=None):
    if game['names']:
        if stats is None:
            stats = get_stats(game)
        users = sorted(stats.values(), key=lambda s: s['right'], reverse=True)
        players = '\n\n' + '\n'.join(f'{u["name"]}: ✔️{u["right"]} ❌{u["wrong"]}' for u in users)
    else:
        players = ''
    bot.edit_message_text(
        lang.gallows.format(
            result=result,
            word=word,
            attempts='\nПопытки: ' + ', '.join(game['wrong']) if game['wrong'] else '',
            players=players
        ) % stickman[len(game['wrong'])],
        chat_id=game['chat'],
        message_id=game['message_id'],
        parse_mode='HTML'
    )


class GameResult(Enum):
    WIN = auto()
    LOSE = auto()


def end_game(game, game_result):
    if game_result == GameResult.WIN:
        result = 'Вы победили!'
    elif game_result == GameResult.LOSE:
        result = 'Вы проиграли.'
    stats = get_stats(game)
    set_gallows(game, result, ' '.join(list(game['word'])), stats=stats)
    for id, s in stats.items():
        increments = {
            'gallows.right': s['right'],
            'gallows.wrong': s['wrong'],
            'gallows.total': 1
        }
        if game_result == GameResult.WIN and s['right']:
            increments['gallows.win'] = 1
        database.stats.update_one(
            {'id': id, 'chat': game['chat']},
            {'$inc': increments},
            upsert=True
        )
    database.games.delete_one({'_id': game['_id']})


def gallows_suggestion(suggestion, game, user, message_id):
    game['names'][user['id']] = user['name']

    if len(suggestion) > 1:
        if re.search(r'\b{}\b'.format(game['word']), suggestion):
            for ch in game['word']:
                if ch not in game['right']:
                    game['right'][ch] = user['id']
            end_game(game, GameResult.WIN)
        return

    if not 'А' <= suggestion <= 'я':
        return

    if suggestion in game['wrong'] or suggestion in game['right']:
        bot.send_message(game['chat'], 'Эта буква уже выбиралась.')
        return

    word = list(game['word'])
    word_in_underlines = []
    has_letter = False
    for ch in word:
        if ch == suggestion:
            word_in_underlines.append(ch)
            has_letter = True
        elif ch in game['right']:
            word_in_underlines.append(ch)
        else:
            word_in_underlines.append('_')

    bot.safely_delete_message(chat_id=game['chat'], message_id=message_id)

    if has_letter:
        game['right'][suggestion] = user['id']
        if word_in_underlines == word:
            end_game(game, GameResult.WIN)
            return
        update = {
            f'right.{suggestion}': user['id'],
            f'names.{user["id"]}': user['name']
        }
    else:
        game['wrong'][suggestion] = user['id']
        if len(game['wrong']) >= len(stickman) - 1:
            end_game(game, GameResult.LOSE)
            return
        update = {
            f'wrong.{suggestion}': user['id'],
            f'names.{user["id"]}': user['name']
        }

    database.games.update_one({'_id': game['_id']}, {'$set': update})
    set_gallows(game, '', ' '.join(word_in_underlines))


def stop_g(game, reason):
    bot.try_to_send_message(
        game['chat'],
        f'Игра окончена! {reason}\n\nРоли были распределены следующим образом:\n' +
        '\n'.join([f'{i + 1}. {p["name"]} - {role_titles[p.get("role", "?")]}' for i, p in enumerate(game['players'])])
    )
    database.games.delete_one({'_id': game['_id']})


def get_name(user):
    return '@' + user.username if user.username else user.first_name


def get_full_name(user):
    result = user.first_name
    if user.last_name:
        result += ' ' + user.last_name
    return result


def user_object(user):
    return {'id': user.id, 'name': get_name(user), 'full_name': get_full_name(user)}


def command_regexp(command):
    return f'^/{command}(@{bot.get_me().username})?$'


@bot.message_handler(regexp=command_regexp('help'))
@bot.message_handler(func=lambda message: message.chat.type == 'private', commands=['start'])
def start_command(message, *args, **kwargs):
    answer = (
        f'Привет, я {bot.get_me().first_name}!\n'
        'Я умею создавать игры в мафию в группах и супергруппах.\n'
        'Инструкция и исходный код: https://gitlab.com/r4rdsn/mafia_host_bot\n'
        'По всем вопросам пишите на https://t.me/r4rdsn'
    )
    bot.send_message(message.chat.id, answer)


def get_mafia_score(stats):
    return 2 * stats.get('win', 0) - stats['total']


def get_croco_score(stats):
    result = 3 * stats['croco'].get('win', 0)
    result += stats['croco'].get('guesses', 0)
    result -= stats['croco'].get('cheat', 0)
    return result / 25


@bot.message_handler(regexp=command_regexp('stats'))
def stats_command(message, *args, **kwargs):
    stats = database.stats.find_one({'id': message.from_user.id, 'chat': message.chat.id})

    if not stats:
        bot.send_message(message.chat.id, f'Статистика {get_name(message.from_user)} пуста.')
        return

    paragraphs = []

    if 'total' in stats:
        win = stats.get('win', 0)
        answer = (
            f'Счёт {get_name(message.from_user)} в мафии: {get_mafia_score(stats)}\n'
            f'Побед: {win}/{stats["total"]} ({100 * win // stats["total"]}%)'
        )
        roles = []
        for role, title in role_titles.items():
            if role in stats:
                role_win = stats[role].get('win', 0)
                roles.append({
                    'title': title,
                    'total': stats[role]['total'],
                    'win': role_win,
                    'rate': 100 * role_win // stats[role]['total']
                })
        for role in sorted(roles, key=lambda s: s['rate'], reverse=True):
            answer += (
                f'\n{role["title"].capitalize()}: '
                f'побед - {role.get("win", 0)}/{role["total"]} ({role["rate"]}%)'
            )
        paragraphs.append(answer)

    if 'croco' in stats:
        answer = f'Счёт {get_name(message.from_user)} в крокодиле: {get_croco_score(stats)}'
        total = stats['croco'].get('total')
        if total:
            win = stats['croco'].get('win', 0)
            answer += f'\nПобед: {win}/{total} ({100 * win // total}%)'
        guesses = stats['croco'].get('guesses')
        if guesses:
            answer += f'\nУгадано: {guesses}'
        paragraphs.append(answer)

    if 'gallows' in stats:
        right = stats['gallows'].get('right', 0)
        wrong = stats['gallows'].get('wrong', 0)
        win = stats['gallows'].get('win', 0)
        total = stats['gallows']['total']
        answer = f'Угадано букв в виселице: {right}/{right + wrong} ({100 * right // (right + wrong)}%)'
        answer += f'\nПобед: {win}/{total} ({100 * win // total}%)'
        paragraphs.append(answer)

    bot.send_message(message.chat.id, '\n\n'.join(paragraphs))


def update_rating(rating, name, score, maxlen):
    place = None
    for i, (_, rating_score) in enumerate(rating):
        if score > rating_score:
            place = i
            break
    if place is not None:
        rating.insert(place, (name, score))
        if len(rating) > maxlen:
            rating.pop(-1)
    elif len(rating) < maxlen:
        rating.append((name, score))


def get_rating_list(rating):
    return '\n'.join(f'{i + 1}. {n}: {s}' for i, (n, s) in enumerate(rating))


@bot.message_handler(regexp=command_regexp('rating'))
def rating_command(message, *args, **kwargs):
    chat_stats = database.stats.find({'chat': message.chat.id})

    if not chat_stats:
        bot.send_message(message.chat.id, 'Статистика чата пуста.')
        return

    mafia_rating = []
    croco_rating = []
    for stats in chat_stats:
        if 'total' in stats:
            update_rating(mafia_rating, stats['name'], get_mafia_score(stats), 5)
        if 'croco' in stats:
            update_rating(croco_rating, stats['name'], get_croco_score(stats), 3)

    paragraphs = []
    if mafia_rating:
        paragraphs.append('Рейтинг игроков в мафию:\n' + get_rating_list(mafia_rating))
    if croco_rating:
        paragraphs.append('Рейтинг игроков в крокодила:\n' + get_rating_list(croco_rating))

    bot.send_message(message.chat.id, '\n\n'.join(paragraphs))


@bot.group_message_handler(regexp=command_regexp('croco'))
def play_croco(message, game, *args, **kwargs):
    if game:
        bot.send_message(message.chat.id, 'Игра в этом чате уже идёт.')
        return
    word = croco.get_word()[:-2]
    id = str(uuid4())[:8]
    keyboard = InlineKeyboardMarkup()
    keyboard.add(
        InlineKeyboardButton(
            text='Получить слово',
            callback_data=f'get_word {id}'
        )
    )
    name = get_name(message.from_user)
    database.games.insert_one({
        'game': 'croco',
        'id': id,
        'player': message.from_user.id,
        'name': name,
        'full_name': get_full_name(message.from_user),
        'word': word,
        'chat': message.chat.id,
        'time': time() + 60,
        'stage': 0
    })
    bot.send_message(
        message.chat.id,
        f'Игра началась! {name.capitalize()}, у тебя есть две минуты, чтобы объяснить слово.',
        reply_markup=keyboard
    )


@bot.group_message_handler(regexp=command_regexp('gallows'))
def play_gallows(message, game, *args, **kwargs):
    if game:
        if game['game'] == 'gallows':
            bot.send_message(message.chat.id, 'Игра в этом чате уже идёт.', reply_to_message_id=game['message_id'])
        else:
            bot.send_message(message.chat.id, 'Игра в этом чате уже идёт.')
        return
    word = croco.get_word()[:-2]
    sent_message = bot.send_message(
        message.chat.id,
        lang.gallows.format(
            result='', word=' '.join(['_'] * len(word)), attempts='', players=''
        ) % gallows.stickman[0],
        parse_mode='HTML'
    )
    database.games.insert_one({
        'game': 'gallows',
        'chat': message.chat.id,
        'word': word,
        'wrong': {},
        'right': {},
        'names': {},
        'message_id': sent_message.message_id
    })


@bot.callback_query_handler(func=lambda call: call.data.startswith('get_word'))
def get_word(call):
    game = database.games.find_one(
        {'game': 'croco', 'id': call.data.split()[1], 'player': call.from_user.id}
    )
    if game:
        bot.answer_callback_query(
            callback_query_id=call.id,
            show_alert=True,
            text=f'Твоё слово: {game["word"]}.'
        )
    else:
        bot.answer_callback_query(
            callback_query_id=call.id,
            show_alert=False,
            text='Ты не можешь получить слово для этой игры.'
        )


@bot.callback_query_handler(func=lambda call: call.data == 'take card')
def take_card(call):
    player_game = database.games.find_one({
        'game': 'mafia',
        'stage': -4,
        'players.id': call.from_user.id,
        'chat': call.message.chat.id,
    })

    if player_game:
        player_index = next(i for i, p in enumerate(player_game['players']) if p['id'] == call.from_user.id)
        player_object = player_game['players'][player_index]

        if player_object.get('role') is None:
            keyboard = InlineKeyboardMarkup()
            keyboard.add(
                InlineKeyboardButton(
                    text='🃏 Вытянуть карту',
                    callback_data='take card'
                )
            )

            player_role = player_game['cards'][player_index]

            player_game = database.games.find_one_and_update(
                {'_id': player_game['_id']},
                {'$set': {f'players.{player_index}.role': player_role}},
                return_document=ReturnDocument.AFTER
            )

            bot.answer_callback_query(
                callback_query_id=call.id,
                show_alert=True,
                text=f'Твоя роль - {role_titles[player_role]}.'
            )

            players_without_roles = [i + 1 for i, p in enumerate(player_game['players']) if p.get('role') is None]

            if len(players_without_roles) > 0:
                bot.edit_message_text(
                    lang.take_card.format(
                        order=format_roles(player_game),
                        not_took=', '.join(map(str, players_without_roles))),
                    chat_id=call.message.chat.id,
                    message_id=call.message.message_id,
                    reply_markup=keyboard
                )

            else:
                database.games.update_one(
                    {'_id': player_game['_id']},
                    {'$set': {'order': []}}
                )

                bot.edit_message_text(
                    'Порядок игроков для игры следующий:\n\n' + format_roles(player_game),
                    chat_id=call.message.chat.id,
                    message_id=call.message.message_id,
                )

                go_to_next_stage(player_game, inc=2)

        else:
            bot.answer_callback_query(
                callback_query_id=call.id,
                show_alert=False,
                text='У тебя уже есть роль.'
            )

    else:
        bot.answer_callback_query(
            callback_query_id=call.id,
            show_alert=False,
            text='Ты сейчас не играешь в игру в этой конфе.'
        )


@bot.callback_query_handler(func=lambda call: call.data == 'mafia team')
def mafia_team(call):
    player_game = database.games.find_one({
        'game': 'mafia',
        'players': {'$elemMatch': {
            'id': call.from_user.id,
            'role': {'$in': ['don', 'mafia']},
        }},
        'chat': call.message.chat.id
    })

    if player_game:
        bot.answer_callback_query(
            callback_query_id=call.id,
            show_alert=True,
            text='Ты играешь в следующей команде:\n' +
                 format_roles(player_game, True, lambda p: p['role'] in ('don', 'mafia')))

    else:
        bot.answer_callback_query(
            callback_query_id=call.id,
            show_alert=False,
            text='Ты не можешь знакомиться с командой мафии.'
        )


@bot.callback_query_handler(func=lambda call: call.data.startswith('check don'))
def check_don(call):
    player_game = database.games.find_one({
        'game': 'mafia',
        'stage': 5,
        'players': {'$elemMatch': {
            'alive': True,
            'role': 'don',
            'id': call.from_user.id
        }},
        'chat': call.message.chat.id
    })

    if player_game and call.from_user.id not in player_game['played']:
        check_player = int(re.match(r'check don (\d+)', call.data).group(1)) - 1

        bot.answer_callback_query(
            callback_query_id=call.id,
            show_alert=True,
            text=f'Да, игрок под номером {check_player + 1} - {role_titles["sheriff"]}'
            if player_game['players'][check_player]['role'] == 'sheriff' else
            f'Нет, игрок под номером {check_player + 1} - не {role_titles["sheriff"]}'
        )

        database.games.update_one({'_id': player_game['_id']}, {'$addToSet': {'played': call.from_user.id}})

    else:
        bot.answer_callback_query(
            callback_query_id=call.id,
            show_alert=False,
            text='Ты не можешь совершать проверку дона.'
        )


@bot.callback_query_handler(func=lambda call: call.data.startswith('check sheriff'))
def check_sheriff(call):
    player_game = database.games.find_one({
        'game': 'mafia',
        'stage': 6,
        'players': {'$elemMatch': {
            'alive': True,
            'role': 'sheriff',
            'id': call.from_user.id
        }},
        'chat': call.message.chat.id
    })

    if player_game and call.from_user.id not in player_game['played']:
        check_player = int(re.match(r'check sheriff (\d+)', call.data).group(1)) - 1

        bot.answer_callback_query(
            callback_query_id=call.id,
            show_alert=True,
            text=f'Да, игрок под номером {check_player + 1} - {role_titles["don"]}'
            if player_game['players'][check_player]['role'] == 'don' else
            f'Да, игрок под номером {check_player + 1} - {role_titles["mafia"]}'
            if player_game['players'][check_player]['role'] == 'mafia' else
            f'Нет, игрок под номером {check_player + 1} - не {role_titles["mafia"]}'
        )

        database.games.update_one({'_id': player_game['_id']}, {'$addToSet': {'played': call.from_user.id}})

    else:
        bot.answer_callback_query(
            callback_query_id=call.id,
            show_alert=False,
            text='Ты не можешь совершать проверку шерифа.'
        )


@bot.callback_query_handler(func=lambda call: call.data.startswith('append to order'))
def append_order(call):
    player_game = database.games.find_one({
        'game': 'mafia',
        'stage': -2,
        'players': {'$elemMatch': {
            'role': 'don',
            'id': call.from_user.id
        }},
        'chat': call.message.chat.id
    })

    if player_game:
        call_player = re.match(r'append to order (\d+)', call.data).group(1)

        database.games.update_one(
            {'_id': player_game['_id']},
            {'$addToSet': {'order': call_player}}
        )

        bot.answer_callback_query(
            callback_query_id=call.id,
            show_alert=False,
            text=f'Игрок под номером {call_player} добавлен в приказ.'
        )

    else:
        bot.answer_callback_query(
            callback_query_id=call.id,
            show_alert=False,
            text='Ты не можешь отдавать приказ дона.'
        )


@bot.callback_query_handler(func=lambda call: call.data.startswith('vote'))
def vote(call):
    player_game = database.games.find_one({
        'game': 'mafia',
        'stage': 1,
        'players': {'$elemMatch': {
            'alive': True,
            'id': call.from_user.id
        }},
        'chat': call.message.chat.id
    })

    if player_game and call.from_user.id not in player_game['played']:
        vote_player = int(re.match(r'vote (\d+)', call.data).group(1)) - 1
        player_index = next(i for i, p in enumerate(player_game['players']) if p['id'] == call.from_user.id)

        game = database.games.find_one_and_update(
            {'_id': player_game['_id']},
            {'$addToSet': {
                'played': call.from_user.id,
                'vote.%d' % vote_player: player_index
            }},
            return_document=ReturnDocument.AFTER
        )

        keyboard = InlineKeyboardMarkup(row_width=8)
        keyboard.add(
            *[InlineKeyboardButton(
                text=f'{i + 1}',
                callback_data=f'vote {i + 1}'
            ) for i, player in enumerate(game['players']) if player['alive']]
        )
        keyboard.add(
            InlineKeyboardButton(
                text='Не голосовать',
                callback_data='vote 0'
            )
        )
        bot.edit_message_text(
            lang.vote.format(vote=get_votes(game)),
            chat_id=game['chat'],
            message_id=game['message_id'],
            reply_markup=keyboard
        )

        bot.answer_callback_query(
            callback_query_id=call.id,
            show_alert=False,
            text=f'Голос отдан против игрока {vote_player + 1}.' if vote_player >= 0 else 'Голос отдан.'
        )

    else:
        bot.answer_callback_query(
            callback_query_id=call.id,
            show_alert=False,
            text='Ты не можешь голосовать.'
        )


@bot.callback_query_handler(func=lambda call: call.data == 'end order')
def end_order(call):
    player_game = database.games.find_one({
        'game': 'mafia',
        'stage': -2,
        'players': {'$elemMatch': {
            'role': 'don',
            'id': call.from_user.id
        }},
        'chat': call.message.chat.id
    })

    if player_game:
        bot.answer_callback_query(
            callback_query_id=call.id,
            show_alert=False,
            text='Приказ записан и будет передан команде мафии.'
        )

        go_to_next_stage(player_game)

    else:
        bot.answer_callback_query(
            callback_query_id=call.id,
            show_alert=False,
            text='Ты не можешь отдавать приказ дона.'
        )


@bot.callback_query_handler(
    func=lambda call: call.data == 'get order',
)
def get_order(call):
    player_game = database.games.find_one({
        'game': 'mafia',
        '$or': [
            {'players': {'$elemMatch': {
                'role': 'don',
                'id': call.from_user.id
            }}},
            {'players': {'$elemMatch': {
                'role': 'mafia',
                'id': call.from_user.id
            }}}
        ],
        'chat': call.message.chat.id
    })

    if player_game:
        if player_game.get('order'):
            order_text = f'Я отдал тебе следующий приказ: {", ".join(player_game["order"])}. Стреляем именно в таком порядке, в противном случае промахнёмся. ~ {role_titles["don"]}'
        else:
            order_text = f'Я не отдал приказа, импровизируем по ходу игры. Главное - стрелять в одних и тех же людей в одну ночь, в противном случае промахнёмся. ~ {role_titles["don"]}'

        bot.answer_callback_query(
            callback_query_id=call.id,
            show_alert=True,
            text=order_text
        )

    else:
        bot.answer_callback_query(
            callback_query_id=call.id,
            show_alert=False,
            text='Ты не можешь получать приказ дона.'
        )


@bot.callback_query_handler(func=lambda call: call.data == 'request interact')
def request_interact(call):
    message_id = call.message.message_id
    required_request = database.requests.find_one({'message_id': message_id})

    if required_request:
        update_dict = {}
        player_object = None
        for player in required_request['players']:
            if player['id'] == call.from_user.id:
                player_object = player
                increment_value = -1
                request_action = '$pull'
                alert_message = 'Ты больше не в игре.'

                break

        if player_object is None:
            if len(required_request['players']) >= config.PLAYERS_COUNT_LIMIT:
                bot.answer_callback_query(
                    callback_query_id=call.id,
                    show_alert=False,
                    text='В игре состоит максимальное количество игроков.'
                )
                return

            player_object = user_object(call.from_user)
            player_object['alive'] = True
            increment_value = 1
            request_action = '$push'
            alert_message = 'Ты теперь в игре.'
            update_dict['$set'] = {'time': time() + config.REQUEST_OVERDUE_TIME}

        update_dict.update(
            {request_action: {'players': player_object},
             '$inc': {'players_count': increment_value}}
        )

        updated_document = database.requests.find_one_and_update(
            {'_id': required_request['_id']},
            update_dict,
            return_document=ReturnDocument.AFTER
        )

        keyboard = InlineKeyboardMarkup()
        keyboard.add(
            InlineKeyboardButton(
                text='Вступить в игру или выйти из игры',
                callback_data='request interact'
            )
        )

        bot.edit_message_text(
            lang.new_request.format(
                owner=updated_document['owner']['name'],
                time=datetime.utcfromtimestamp(updated_document['time']).strftime('%H:%M'),
                order='Игроков нет.' if not updated_document['players_count'] else
                'Игроки:\n' + '\n'.join([f'{i + 1}. {p["name"]}' for i, p in enumerate(updated_document['players'])])
            ),
            chat_id=call.message.chat.id,
            message_id=message_id,
            reply_markup=keyboard
        )

        bot.answer_callback_query(callback_query_id=call.id, show_alert=False, text=alert_message)
    else:
        bot.edit_message_text('Заявка больше не существует.', chat_id=call.message.chat.id, message_id=message_id)


@bot.group_message_handler(regexp=command_regexp('create'))
def create(message, *args, **kwargs):
    existing_request = database.requests.find_one({'chat': message.chat.id})
    if existing_request:
        bot.send_message(message.chat.id, 'В этом чате уже есть игра!',
                         reply_to_message_id=existing_request['message_id'])
        return
    existing_game = database.games.find_one({'chat': message.chat.id, 'game': 'mafia'})
    if existing_game:
        bot.send_message(message.chat.id, 'В этом чате уже идёт игра!')
        return

    keyboard = InlineKeyboardMarkup()
    keyboard.add(
        InlineKeyboardButton(
            text='Вступить в игру или выйти из игры',
            callback_data='request interact'
        )
    )

    player_object = user_object(message.from_user)
    player_object['alive'] = True
    request_overdue_time = time() + config.REQUEST_OVERDUE_TIME

    answer = lang.new_request.format(
        owner=get_name(message.from_user),
        time=datetime.utcfromtimestamp(request_overdue_time).strftime('%H:%M'),
        order=f'Игроки:\n1. {player_object["name"]}'
    )
    sent_message = bot.send_message(message.chat.id, answer, reply_markup=keyboard)

    database.requests.insert_one({
        'id': str(uuid4())[:8],
        'owner': player_object,
        'players': [player_object],
        'time': request_overdue_time,
        'chat': message.chat.id,
        'message_id': sent_message.message_id,
        'players_count': 1
    })


@bot.group_message_handler(regexp=command_regexp('start'))
def start_game(message, *args, **kwargs):
    req = database.requests.find_and_modify(
        {
            'owner.id': message.from_user.id,
            'chat': message.chat.id,
            'players_count': {'$gte': config.PLAYERS_COUNT_TO_START}
        },
        new=False,
        remove=True
    )
    if req is not None:
        players_count = req['players_count']

        cards = ['mafia'] * (players_count // 3 - 1) + ['don', 'sheriff']
        cards += ['peace'] * (players_count - len(cards))
        random.shuffle(cards)

        keyboard = InlineKeyboardMarkup()
        keyboard.add(
            InlineKeyboardButton(
                text='🃏 Вытянуть карту',
                callback_data='take card'
            )
        )

        stage_number = min(stages.keys())

        message_id = bot.send_message(
            message.chat.id,
            lang.take_card.format(
                order='\n'.join([f'{i + 1}. {p["name"]}' for i, p in enumerate(req['players'])]),
                not_took=', '.join(map(str, range(1, len(req['players']) + 1))),
            ),
            reply_markup=keyboard
        ).message_id

        database.games.insert_one({
            'game': 'mafia',
            'chat': req['chat'],
            'id': req['id'],
            'stage': stage_number,
            'day_count': 0,
            'players': req['players'],
            'cards': cards,
            'next_stage_time': time() + stages[stage_number]['time'],
            'message_id': message_id,
            'don': [],
            'vote': {},
            'shots': [],
            'played': []
        })

    else:
        bot.send_message(message.chat.id, 'У тебя нет заявки на игру, которую возможно начать.')


@bot.group_message_handler(regexp=command_regexp('cancel'))
def cancel(message, *args, **kwargs):
    req = database.requests.find_one_and_delete({
        'owner.id': message.from_user.id,
        'chat': message.chat.id
    })
    if req:
        answer = 'Твоя заявка удалена.'
    else:
        answer = 'У тебя нет заявки на игру.'
    bot.send_message(message.chat.id, answer)


def create_poll(message, game, poll_type, suggestion):
    if not game or game['stage'] not in (0, -4):
        return

    check_roles = game['stage'] == 0

    existing_poll = database.polls.find_one({
        'chat': message.chat.id,
        'type': poll_type
    })
    if existing_poll:
        bot.send_message(
            message.chat.id,
            'В этом чате уже идёт голосование!',
            reply_to_message_id=existing_poll['message_id']
        )
        return

    poll = {
        'chat': message.chat.id,
        'type': poll_type,
        'creator': get_name(message.from_user),
        'check_roles': check_roles,
        'votes': [message.from_user.id],
    }

    keyboard = InlineKeyboardMarkup()
    if check_roles:
        peace_team = set()
        mafia_team = set()

        for player in game['players']:
            if player['alive']:
                if player['role'] in ('don', 'mafia'):
                    mafia_team.add(player['id'])
                else:
                    peace_team.add(player['id'])

        peace_votes = 0
        mafia_votes = 0
        if message.from_user.id in peace_team:
            peace_votes += 1
        else:
            mafia_votes += 1

        poll['peace_count'] = peace_votes
        poll['peace_required'] = 2 * len(peace_team) // 3
        poll['mafia_count'] = mafia_votes
        poll['mafia_required'] = 2 * len(mafia_team) // 3

    else:
        poll['count'] = 1
        poll['required'] = 2 * len(game['players']) // 3

    keyboard.add(
        InlineKeyboardButton(
            text='Проголосовать',
            callback_data='poll'
        )
    )

    answer = f'{poll["creator"]} предлагает {suggestion}.'
    poll['message_id'] = bot.send_message(message.chat.id, answer, reply_markup=keyboard).message_id
    database.polls.insert_one(poll)


@bot.group_message_handler(regexp=command_regexp('end'))
def force_game_end(message, game, *args, **kwargs):
    create_poll(message, game, 'end', 'закончить игру')


@bot.group_message_handler(regexp=command_regexp('skip'))
def skip_current_stage(message, game, *args, **kwargs):
    create_poll(message, game, 'skip', 'пропустить текущую стадию')


@bot.callback_query_handler(func=lambda call: call.data == 'poll')
def poll_vote(call):
    message_id = call.message.message_id
    poll = database.polls.find_one({'message_id': message_id})

    if not poll:
        bot.edit_message_text(
            'Голосование больше не существует.',
            chat_id=call.message.chat.id,
            message_id=message_id
        )
        return

    if call.from_user.id in poll['votes']:
        bot.answer_callback_query(
            callback_query_id=call.id,
            show_alert=False,
            text='Твой голос уже был учтён.',
        )
        return

    player_game = database.games.find_one({
        'game': 'mafia',
        'players': {'$elemMatch': {
            'alive': True,
            'id': call.from_user.id
        }},
        'chat': call.message.chat.id
    })

    if not player_game:
        bot.answer_callback_query(
            callback_query_id=call.id,
            show_alert=False,
            text='Ты не можешь голосовать.',
        )
        return

    increment_value = {}

    if poll['check_roles']:
        mafia_count = poll['mafia_count']
        peace_count = poll['peace_count']

        for player in player_game['players']:
            if player['id'] == call.from_user.id:
                if player['role'] in ('don', 'mafia'):
                    increment_value['mafia_count'] = 1
                    mafia_count += 1
                else:
                    increment_value['peace_count'] = 1
                    peace_count += 1

                poll_condition = mafia_count > poll['mafia_required'] and peace_count >= poll['peace_required']
                break
    else:
        increment_value['count'] = 1
        poll_condition = poll['count'] + 1 > poll['required']

    if poll_condition:
        bot.edit_message_reply_markup(
            chat_id=call.message.chat.id,
            message_id=message_id
        )
        if poll['type'] == 'skip':
            go_to_next_stage(player_game)
        elif poll['type'] == 'end':
            stop_g(player_game, reason='Игроки проголосовали за окончание игры.')
            return

    database.polls.update_one(
        {'_id': poll['_id']},
        {
            '$addToSet': {'votes': call.from_user.id},
            '$inc': increment_value
        }
    )

    bot.answer_callback_query(
        callback_query_id=call.id,
        show_alert=False,
        text='Голос учтён.'
    )


@bot.callback_query_handler(func=lambda call: call.data.startswith('shot'))
def callback_inline(call):
    player_game = database.games.find_one({
        'game': 'mafia',
        'stage': 4,
        'players': {'$elemMatch': {
            'alive': True,
            'role': {'$in': ['don', 'mafia']},
            'id': call.from_user.id
        }},
        'chat': call.message.chat.id
    })

    if player_game and call.from_user.id not in player_game['played']:
        victim = int(call.data.split()[1]) - 1
        database.games.update_one(
            {'_id': player_game['_id']},
            {
                '$addToSet': {'played': call.from_user.id},
                '$push': {'shots': victim}
            }
        )

        bot.answer_callback_query(
            callback_query_id=call.id,
            show_alert=False,
            text=f'Выстрел произведён в игрока {victim + 1}'
        )

    else:
        bot.answer_callback_query(
            callback_query_id=call.id,
            show_alert=False,
            text='Ты не можешь участвовать в стрельбе'
        )


@bot.message_handler(
    func=lambda message: message.from_user.id == config.ADMIN_ID,
    regexp=command_regexp('reset')
)
def reset(message, *args, **kwargs):
    database.games.delete_many({})
    bot.send_message(message.chat.id, 'База игр сброшена!')


@bot.message_handler(
    func=lambda message: message.from_user.id == config.ADMIN_ID,
    regexp=command_regexp('database')
)
def print_database(message, *args, **kwargs):
    print(list(database.games.find()))
    bot.send_message(message.chat.id, 'Все документы базы данных игр выведены в терминал!')


@bot.group_message_handler(content_types=['text'])
def game_suggestion(message, game, *args, **kwargs):
    if not game or message.text is None:
        return
    suggestion = message.text.lower().replace('ё', 'е')
    user = user_object(message.from_user)
    if game['game'] == 'gallows':
        return gallows.gallows_suggestion(suggestion, game, user, message.message_id)
    elif game['game'] == 'croco':
        return croco.croco_suggestion(suggestion, game, user, message.message_id)


@bot.group_message_handler()
def default_handler(message, *args, **kwargs):
    pass


def add_stage(number, time=None, delete=False):
    def decorator(func):
        stages[number] = {'time': time, 'func': func, 'delete': delete}
        return func

    return decorator


def go_to_next_stage(game, inc=1):
    database.polls.delete_many({'chat': game['chat']})

    stage_number = 0 if game['stage'] == max(stages.keys()) + 1 - inc else game['stage'] + inc
    stage = stages[stage_number]
    if stage['delete']:
        database.games.delete_one({'_id': game['_id']})
        new_game = game
    else:
        time_inc = stage['time'](game) if callable(stage['time']) else stage['time']
        new_game = database.games.find_one_and_update(
            {'_id': game['_id']},
            {
                '$set': {
                    'next_stage_time': time() + (time_inc if isinstance(time_inc, (int, float)) else 0),
                    'stage': stage_number,
                    'played': []
                },
                '$inc': {'day_count': int(stage_number == 0)}},
            return_document=ReturnDocument.AFTER
        )

    try:
        stage['func'](new_game)
    except ApiException as exception:
        if exception.result.status_code == 403:
            database.games.delete_one({'_id': game['_id']})
            return

    return new_game


def format_roles(game, show_roles=False, condition=lambda p: p.get('alive', True)):
    return '\n'.join(
        [f'{i + 1}. {p["name"]}{" - " + role_titles[p["role"]] if show_roles else ""}'
         for i, p in enumerate(game['players']) if condition(p)]
    )


@add_stage(-4, 90)
def first_stage():
    pass


@add_stage(-3, delete=True)
def cards_not_taken(game):
    bot.edit_message_text(
        'Игра окончена! Игроки не взяли свои карты.',
        chat_id=game['chat'],
        message_id=game['message_id']
    )


@add_stage(-2, 60)
def set_order(game):
    if not any(p['role'] == 'mafia' for p in game['players']):
        go_to_next_stage(game, inc=2)
        return

    keyboard = InlineKeyboardMarkup(row_width=8)
    keyboard.add(
        *[InlineKeyboardButton(
            text=f'{i + 1}',
            callback_data=f'append to order {i + 1}'
        ) for i, player in enumerate(game['players'])]
    )
    keyboard.row(
        InlineKeyboardButton(
            text='Познакомиться с командой',
            callback_data='mafia team'
        )
    )
    keyboard.row(
        InlineKeyboardButton(
            text='Закончить выбор',
            callback_data='end order'
        )
    )

    message_id = bot.send_message(
        game['chat'],
        f'{role_titles["don"].capitalize()}, тебе предстоит сделать свой выбор и определить порядок выстрелов твоей команды.\nДля этого последовательно нажимай на номера игроков, а после этого нажми на кнопку "Закончить выбор".',
        reply_markup=keyboard
    ).message_id

    database.games.update_one({'_id': game['_id']}, {'$set': {'message_id': message_id}})


@add_stage(-1, 5)
def get_order(game):
    keyboard = InlineKeyboardMarkup()
    keyboard.add(
        InlineKeyboardButton(
            text='✉ Получить приказ',
            callback_data='get order'
        )
    )

    bot.edit_message_text(
        f'{role_titles["don"].capitalize()} записал приказ. {role_titles["mafia"].capitalize()}, получите конверт со своим заданием!',
        chat_id=game['chat'],
        message_id=game['message_id'],
        reply_markup=keyboard
    )


@add_stage(0, lambda g: 90 + max(0, sum(p['alive'] for p in g['players']) - 4) * 35)
def discussion(game):
    if game['day_count'] > 1 and game.get('victim') is None:
        bot.edit_message_text(
            lang.morning_message.format(
                peaceful_night=(
                    'Доброе утро, город!\n'
                    'Этой ночью обошлось без смертей.\n'
                ),
                day=game['day_count'],
                order=format_roles(game)
            ),
            chat_id=game['chat'],
            message_id=game['message_id']
        )
    else:
        if game['day_count'] > 1:
            database.games.update_one({'_id': game['_id']}, {'$unset': {'victim': True}})
        bot.send_message(
            game['chat'],
            lang.morning_message.format(
                peaceful_night='',
                day=game['day_count'],
                order=format_roles(game)
            ),
        )


def get_votes(game):
    names = [(0, 'Не голосовать')] + [(i + 1, p['name']) for i, p in enumerate(game['players']) if p['alive']]
    return '\n'.join([
        f'{i}. {name}' + (
            (': ' + ', '.join(str(v + 1) for v in game['vote'][str(i - 1)]))
            if str(i - 1) in game['vote'] else ''
        ) for i, name in names
    ])


@add_stage(1, 30)
def vote(game):
    keyboard = InlineKeyboardMarkup(row_width=8)
    keyboard.add(
        *[InlineKeyboardButton(
            text=f'{i + 1}',
            callback_data=f'vote {i + 1}'
        ) for i, player in enumerate(game['players']) if player['alive']]
    )
    keyboard.add(
        InlineKeyboardButton(
            text='Не голосовать',
            callback_data='vote 0'
        )
    )

    message_id = bot.send_message(
        game['chat'],
        lang.vote.format(vote=get_votes(game)),
        reply_markup=keyboard
    ).message_id

    database.games.update_one({'_id': game['_id']}, {'$set': {'message_id': message_id}})


@add_stage(2, 20)
def last_words_criminal(game):
    criminal = None
    if game['vote']:
        most_voted = max(game['vote'].values(), key=len)
        candidates = [int(i) for i, votes in game['vote'].items() if len(votes) == len(most_voted)]
        if len(candidates) == 1 and candidates[0] >= 0:
            criminal = candidates[0]

    bot.edit_message_text(
        f'Народным голосованием в тюрьму был посажен игрок {criminal + 1} ({game["players"][criminal]["name"]}).'
        if criminal is not None else 'Город не выбрал преступника.',
        chat_id=game['chat'],
        message_id=game['message_id']
    )

    update_dict = {'$set': {'vote': {}}}

    if criminal is not None:
        update_dict['$set'][f'players.{criminal}.alive'] = False
        update_dict['$set']['victim'] = game['players'][criminal]['id']

    database.games.update_one({'_id': game['_id']}, update_dict)


@add_stage(3, 5)
def night(game):
    message_id = bot.send_message(
        game['chat'],
        f'Наступает ночь. Город засыпает. {role_titles["mafia"].capitalize()}, приготовьтесь к выстрелу...'
    ).message_id
    database.games.update_one(
        {'_id': game['_id']},
        {
            '$unset': {'victim': True},
            '$set': {'message_id': message_id}
        }
    )


@add_stage(4, 5)
def shooting_stage(game):
    players = [(i, player) for i, player in enumerate(game['players']) if player['alive']]
    random.shuffle(players)

    keyboard = InlineKeyboardMarkup(row_width=8)
    keyboard.add(
        *[InlineKeyboardButton(
            text=f'{i + 1}',
            callback_data=f'shot {i + 1}'
        ) for i, player in players]
    )

    bot.edit_message_text(
        f'{role_titles["mafia"].capitalize()} выбирает жертву.\n' + format_roles(game),
        chat_id=game['chat'],
        message_id=game['message_id'],
        reply_markup=keyboard
    )


@add_stage(5, 10)
def don_stage(game):
    keyboard = InlineKeyboardMarkup(row_width=8)
    keyboard.add(
        *[InlineKeyboardButton(
            text=f'{i + 1}',
            callback_data=f'check don {i + 1}'
        ) for i, player in enumerate(game['players']) if player['alive']]
    )

    bot.edit_message_text(
        f'{role_titles["mafia"].capitalize()} засыпает. {role_titles["don"].capitalize()} совершает свою проверку.\n' + format_roles(
            game),
        chat_id=game['chat'],
        message_id=game['message_id'],
        reply_markup=keyboard
    )


@add_stage(6, 10)
def sheriff_stage(game):
    keyboard = InlineKeyboardMarkup(row_width=8)
    keyboard.add(
        *[InlineKeyboardButton(
            text=f'{i + 1}',
            callback_data=f'check sheriff {i + 1}'
        ) for i, player in enumerate(game['players']) if player['alive']]
    )

    bot.edit_message_text(
        f'{role_titles["don"].capitalize()} засыпает. Просыпается {role_titles["sheriff"]} и совершает свою проверку.\n{format_roles(game)}',
        chat_id=game['chat'],
        message_id=game['message_id'],
        reply_markup=keyboard
    )


@add_stage(7, 20)
def last_words_victim(game):
    update_dict = {'$set': {'shots': []}}

    mafia_shot = False
    if len(set(game['shots'])) == 1 and len(game['shots']) == sum(
            p['role'] in ('don', 'mafia') and p['alive'] for p in game['players']):
        victim = game['shots'][0]
        if game['players'][victim]['alive']:
            mafia_shot = True
            update_dict['$set'][f'players.{victim}.alive'] = False
            update_dict['$set']['victim'] = game['players'][victim]['id']

    database.games.update_one({'_id': game['_id']}, update_dict)

    if not mafia_shot:
        go_to_next_stage(game)
        return

    bot.edit_message_text(
        f'Доброе утро, город!\nПечальные новости: этой ночью был убит игрок {victim + 1} ({game["players"][victim]["name"]}).',
        chat_id=game['chat'],
        message_id=game['message_id']
    )


new_request = (
    'Игра создана.\n'
    'Создатель игры: {owner}.\n'
    'Время удаления игры: {time} UTC.\n'
    '{order}'
)

take_card = (
    'Игра начата!\n\n'
    'Порядок игроков следующий:\n'
    '{order}\n\n'
    'Игроки с номерами [{not_took}], разбираем карты!'
)

morning_message = (
    '{peaceful_night}'
    'Идёт день {day}.\n'
    'У вас есть время, чтобы решить, за кого вы проголосуете сегодняшним вечером.\n'
    '──────────────────\n'
    'Игроки:\n'
    '{order}'
)

vote = (
    'Город, настало время для голосования!\n'
    '{vote}'
)

gallows = (
    '<code>'
    '___________\n'
    '|         |\n'
    '|        %s\n'
    '|        %s\n'
    '|        %s\n'
    '|\n'
    '|'
    '</code>\n'
    '{result}\nСлово: {word}{attempts}{players}'
)

role_titles = {
    'don': 'дон мафии',
    'mafia': 'мафия',
    'sheriff': 'шериф',
    'peace': 'мирный житель'
}

stickman = [
    ('', '', ''),
    (' 0', '', ''),
    (' 0', ' |', ''),
    (' 0', '/|', ''),
    (' 0', '/|\\', ''),
    (' 0', '/|\\', '/'),
    (' 0', '/|\\', '/ \\')
]
client = MongoClient()
database = client.mafia_host_bot
BASE_SIZE = getsize(config.WORD_BASE)
bot = MafiaHostBot(config.TOKEN, skip_pending=config.SKIP_PENDING)


main()
