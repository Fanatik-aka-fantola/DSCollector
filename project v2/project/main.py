from flask import Flask, session, current_app, render_template, abort, flash, redirect, request, jsonify, make_response, url_for, Blueprint
from flask_login import LoginManager, login_user, login_required, logout_user, current_user
from functools import lru_cache
from multiprocessing import Process
from functools import wraps
from sqlalchemy.exc import SQLAlchemyError


# 1. Сначала импортируем ВСЕ модели
from project.models.users import User
from project.models.text_data import TextData
from project.models.TelegramModel import TelegramChat


# 2. Потом всё остальное
from project.data.db_session import create_session, global_init
from project.data.telegram_parser import TelegramBot
from project.data.text_processor import TextProcessor
from project.forms.profile_form import ProfileForm
from forms.user import RegisterForm, LoginForm
from project.data.sentiment_analyzer import RussianSentimentAnalyzer
import os
import json
from datetime import datetime
from huggingface_hub.utils import disable_progress_bars

# Отключаем предупреждения о resume_download
import warnings
warnings.filterwarnings("ignore", category=UserWarning, module="xformers")
warnings.filterwarnings("ignore", category=FutureWarning, module="huggingface_hub")


app = Flask(__name__)
bot = TelegramBot("7678402317:AAGJcI6Z8wQvrzpGJsIE-f_-fVa5HSVJ4ZA")
os.environ["HF_HUB_DISABLE_SYMLINKS_WARNING"] = "1"
app.config['SECRET_KEY'] = 'yandexlyceum_secret_key'

# 3. Только теперь инициализируем БД (модели уже загружены)
global_init("db/blogs.db")

login_manager = LoginManager()
login_manager.init_app(app)
processor = TextProcessor()


@login_manager.user_loader
def load_user(user_id):
    db_sess = create_session()
    ret = db_sess.query(User).get(user_id)
    db_sess.close()
    return ret


@app.route('/register', methods=['GET', 'POST'])
def reqister():
    form = RegisterForm()
    if form.validate_on_submit():
        if form.password.data != form.password_again.data:
            return render_template('register.html', title='Регистрация',
                                   form=form,
                                   message="Пароли не совпадают")
        db_sess = create_session()
        if db_sess.query(User).filter(User.email == form.email.data).first():
            return render_template('register.html', title='Регистрация',
                                   form=form,
                                   message="Такой пользователь уже есть")
        user = User(
            name=form.name.data,
            email=form.email.data,
        )
        user.set_password(form.password.data)
        db_sess.add(user)
        db_sess.commit()
        db_sess.close()
        return redirect('/login')
    return render_template('register.html', title='Регистрация', form=form)


@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('collect_data'))

    form = LoginForm()
    if form.validate_on_submit():
        db_sess = create_session()
        user = db_sess.query(User).filter(User.email == form.email.data).first()

        if user and user.check_password(form.password.data):
            login_user(user, remember=form.remember_me.data)
            session.pop('selected_source', None)  # Сбрасываем предыдущий выбор
            db_sess.close()
            return redirect(url_for('collect_data'))

        db_sess.close()
        flash('Неправильный логин или пароль', 'error')

    return render_template('login.html', form=form)


@app.route('/profile', methods=['GET', 'POST'])
@login_required
def profile():
    form = ProfileForm()
    db_sess = create_session()

    try:
        user = db_sess.query(User).filter(User.id == current_user.id).first()
        if not user:
            flash('Пользователь не найден', 'error')
            return redirect(url_for('profile'))

        if request.method == 'POST' and form.validate_on_submit():
            updated = False  # Флаг для отслеживания изменений

            # Обновление имени
            if form.name.data and form.name.data != user.name:
                user.name = form.name.data
                updated = True
                flash('Имя успешно обновлено', 'success')

            # Обновление email
            if form.email.data and form.email.data != user.email:
                if db_sess.query(User).filter(User.email == form.email.data, User.id != current_user.id).first():
                    flash('Этот email уже используется', 'error')
                else:
                    user.email = form.email.data
                    updated = True
                    flash('Email успешно обновлён', 'success')

            # Обновление пароля
            if form.current_password.data and form.new_password.data:
                if user.check_password(form.current_password.data):
                    user.set_password(form.new_password.data)
                    updated = True
                    flash('Пароль успешно изменён', 'success')
                else:
                    flash('Текущий пароль неверен', 'error')

            if updated:
                db_sess.commit()
                flash('Профиль успешно обновлён', 'success')
            else:
                flash('Изменения не внесены', 'info')

            return redirect(url_for('profile'))

    except SQLAlchemyError as e:
        db_sess.rollback()
        flash('Ошибка базы данных. Попробуйте снова.', 'error')
        current_app.logger.error(f"Database error: {str(e)}")
    except Exception as e:
        db_sess.rollback()
        flash(f'Ошибка: {str(e)}', 'error')
        current_app.logger.error(f"Unexpected error: {str(e)}")
    finally:
        db_sess.close()

    return render_template('profile.html', user=current_user, form=form)


@app.route('/generate_verification_code', methods=['POST'])
@login_required
def generate_verification_code():
    db = create_session()
    user = db.query(User).get(current_user.id)

    if user.telegram_id and user.is_telegram_verified:
        return jsonify({"status": "error", "message": "Telegram уже привязан"})

    code = user.generate_telegram_code()
    db.commit()

    return jsonify({
        "status": "success",
        "code": code,
        "expires_at": user.telegram_code_expires.isoformat()
    })


@app.route('/verify_telegram', methods=['POST'])
@login_required
def verify_telegram():
    data = request.get_json()
    if not data or 'telegram_id' not in data or 'code' not in data:
        return jsonify({"status": "error", "message": "Неверные данные"})

    db = create_session()
    user = db.query(User).get(current_user.id)

    # Проверяем, не привязан ли этот Telegram ID к другому аккаунту
    existing_user = db.query(User).filter_by(telegram_id=data['telegram_id']).first()
    if existing_user and existing_user.id != user.id:
        return jsonify({"status": "error", "message": "Этот Telegram уже привязан к другому аккаунту"})

    if user.verify_telegram_code(data['code']):
        user.telegram_id = data['telegram_id']
        db.commit()
        return jsonify({"status": "success"})

    return jsonify({"status": "error", "message": "Неверный или просроченный код"})


@app.route('/unlink_telegram', methods=['POST'])
@login_required
def unlink_telegram():
    db_sess = create_session()
    try:
        user = db_sess.query(User).filter(User.id == current_user.id).first()
        user.telegram_id = None
        user.is_telegram_verified = False
        db_sess.commit()
        flash('Telegram успешно отвязан', 'success')
    except Exception as e:
        db_sess.rollback()
        flash('Ошибка при отвязке Telegram', 'error')
    finally:
        db_sess.close()
    return redirect(url_for('profile'))


@app.route('/check_verification', methods=['GET'])
@login_required
def check_verification():
    db = create_session()
    user = db.query(User).get(current_user.id)
    return jsonify({
        "is_verified": user.is_telegram_verified,
        "telegram_id": user.telegram_id
    })

def telegram_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not current_user.is_telegram_verified:
            flash('Для доступа к этой странице необходимо привязать Telegram аккаунт', 'warning')
            return redirect(url_for('telegram_connect'))
        return f(*args, **kwargs)
    return decorated_function


@app.route('/telegram_connect')
@login_required
def telegram_connect():
    if current_user.is_telegram_verified:
        return redirect(url_for('collect_data'))
    return render_template('telegram_connect.html')


@app.route("/")
def index():
    if current_user.is_authenticated:
        return render_template("collect.html")
    else:
        return render_template("base.html")


@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect("/")


@app.route("/analyze", methods=["POST"])
def analyze_endpoint():
    data = request.json
    text = data["text"]
    source = data.get("source", "unknown")
    chat_id = data.get("chat_id")
    author = data.get("author")

    db_sess = create_session()
    try:
        new_entry = processor.process_text(text, source, chat_id, author)
        db_sess.add(new_entry)
        db_sess.commit()
        db_sess.close()

        return jsonify({
            "id": new_entry.id,
            "text": new_entry.text,
            "sentiment": new_entry.sentiment,
            "confidence": new_entry.sentiment_confidence,
            "created_at": new_entry.created_at.isoformat()
        })
    except Exception as e:
        db_sess.rollback()
        db_sess.close()
        return jsonify({"error": str(e)}), 500


@lru_cache(maxsize=1)
def get_analyzer():
    return RussianSentimentAnalyzer()


@app.route("/collect", methods=["GET", "POST"])
@login_required
def collect_data():
    # Если источник не выбран - перенаправляем на выбор
    if 'selected_source' not in session:
        return redirect(url_for('select_source'))

    db = None
    try:
        db = create_session()

        # Для Telegram проверяем привязку и получаем чаты
        if session['selected_source'] == 'telegram':
            if not current_user.is_telegram_verified:
                flash('Для работы с Telegram необходимо привязать аккаунт', 'warning')
                return redirect(url_for('telegram_connect'))

            chats = db.query(TelegramChat).filter(
                TelegramChat.user_id == current_user.telegram_id
            ).order_by(TelegramChat.title).all()

        else:
            chats = None

        # Обработка POST-запроса
        if request.method == "POST":
            text = request.form.get("text", "").strip()
            if not text:
                flash('Текст не может быть пустым', 'error')
            else:
                # Обработка данных
                processor = TextProcessor()
                new_entry = processor.process_text(
                    text=text,
                    source=session['selected_source'],
                    author=str(current_user.id)
                )
                db.add(new_entry)
                db.commit()
                flash('Данные успешно сохранены', 'success')

        return render_template(
            "collect.html",
            chats=chats,
            source=session['selected_source']
        )

    except Exception as e:
        if db:
            db.rollback()
        flash(f'Ошибка: {str(e)}', 'error')
        current_app.logger.error(f"Error in collect_data: {str(e)}")
        return redirect(url_for('collect_data'))

    finally:
        if db:
            db.close()


@app.route('/other_source')
@login_required
def other_source():
    if session.get('selected_source') != 'other':
        return redirect(url_for('index'))

    return render_template('other_source.html')


@app.route('/select_source', methods=['GET', 'POST'])
@login_required
def select_source():
    # Обработка выбора источника
    if request.method == 'POST' or request.args.get('source'):
        source = request.form.get('source') or request.args.get('source')

        if source not in ['telegram', 'other']:
            flash('Неверный источник данных', 'error')
        else:
            session['selected_source'] = source

            if source == 'telegram' and not current_user.is_telegram_verified:
                return redirect(url_for('telegram_connect'))

            return redirect(url_for('collect_data'))

    # Отображение формы выбора
    return render_template('select_source.html')


@app.route("/add_chat", methods=["POST"])
def add_chat():
    db = create_session()
    chat_id = request.form.get("chat_id", "").strip()

    if not chat_id:
        return redirect(url_for('collect_data',
                                source='telegram',
                                error="Не указан ID чата"))

    # Проверяем существование чата
    if db.query(TelegramChat).filter_by(chat_id=chat_id).first():
        return redirect(url_for('collect_data',
                                source='telegram',
                                error="Чат уже существует"))

    try:
        # Определяем тип чата и название
        if chat_id.startswith('@'):
            chat_type = "private"
            title = f"Приватный чат {chat_id}"
        elif chat_id.lstrip('-').isdigit():
            chat_type = "group" if int(chat_id) < 0 else "private"
            title = f"Группа {chat_id}" if chat_type == "group" else f"ЛС {chat_id}"
        else:
            chat_type = "channel"
            title = f"Канал {chat_id}"

        # Создаем новый объект чата
        new_chat = TelegramChat(
            chat_id=chat_id,
            title=title,
            is_active=True,
            chat_type=chat_type,
            created_at=datetime.now()
        )

        db.add(new_chat)
        db.commit()
        db.close()
        return redirect(url_for('collect_data',
                                source='telegram',
                                success=f"Чат {title} успешно добавлен"))
    except Exception as e:
        db.rollback()
        db.close()
        return redirect(url_for('collect_data',
                                source='telegram',
                                error=f"Ошибка при добавлении чата: {str(e)}"))


@app.route("/toggle_chat", methods=["POST"])
def toggle_chat():
    db = create_session()
    chat_id = request.form.get("chat_id")

    if not chat_id:
        return redirect(url_for('collect_data',
                                source='telegram',
                                error="Не указан ID чата"))

    try:
        chat = db.query(TelegramChat).filter_by(chat_id=chat_id).first()
        if not chat:
            return redirect(url_for('collect_data',
                                    source='telegram',
                                    error="Чат не найден"))

        chat.is_active = not chat.is_active
        db.commit()


        action = "включен" if chat.is_active else "выключен"
        db.close()
        return redirect(url_for('collect_data',
                                source='telegram',
                                success=f"Чат {chat.title} {action}"))
    except Exception as e:
        db.rollback()
        return redirect(url_for('collect_data',
                                source='telegram',
                                error=f"Ошибка переключения: {str(e)}"))


@app.route("/delete_chat", methods=["POST"])
def delete_chat():
    db = create_session()
    chat_id = request.form.get("chat_id")

    if not chat_id:
        return redirect(url_for('collect_data',
                                source='telegram',
                                error="Не указан ID чата"))

    try:
        chat = db.query(TelegramChat).filter_by(chat_id=chat_id).first()
        if not chat:
            return redirect(url_for('collect_data',
                                    source='telegram',
                                    error="Чат не найден"))

        # Удаляем связанные сообщения
        db.query(TextData).filter_by(chat_id=chat_id).delete()

        # Удаляем сам чат
        db.delete(chat)
        db.commit()
        db.close()

        return redirect(url_for('collect_data',
                                source='telegram',
                                success=f"Чат {chat.title} удалён"))
    except Exception as e:
        db.rollback()
        return redirect(url_for('collect_data',
                                source='telegram',
                                error=f"Ошибка удаления: {str(e)}"))


@app.route("/export/<chat_id>")
def export_json(chat_id):
    db = create_session()

    try:
        # Для экспорта всех чатов
        if chat_id == 'all':
            messages = db.query(TextData).filter_by(source='telegram').all()
            filename = f"export_all_{datetime.now().strftime('%Y-%m-%d')}.json"
        # Для конкретного чата
        else:
            chat = db.query(TelegramChat).filter_by(chat_id=chat_id).first()
            if not chat:
                return "Чат не найден", 404

            messages = db.query(TextData).filter_by(
                source='telegram',
                chat_id=chat_id
            ).all()
            filename = f"export_{chat_id}_{datetime.now().strftime('%Y-%m-%d')}.json"

        # Формируем данные для экспорта
        data = [{
            'id': msg.id,
            'chat_id': msg.chat_id,
            'text': msg.text,
            'sentiment': msg.sentiment,
            'sentiment_confidence': msg.sentiment_confidence,
            'created_at': msg.created_at.isoformat() if msg.created_at else None
        } for msg in messages]

        # Создаем JSON-ответ
        response = make_response(json.dumps(data, ensure_ascii=False, indent=2))
        response.headers['Content-Disposition'] = f'attachment; filename={filename}'
        response.headers['Content-type'] = 'application/json; charset=utf-8'
        db.close()
        return response

    except Exception as e:
        app.logger.error(f"Export error: {str(e)}")
        return "Ошибка экспорта", 500


# Добавляем новый роут для управления ботом
@app.route("/bot_control", methods=["POST"])
def bot_control():
    action = request.form.get("action")

    if action == "start":
        # Логика запуска бота
        return "Бот запущен"
    elif action == "stop":
        # Логика остановки бота
        return "Бот остановлен"

    return "Неизвестное действие", 400


def run_bot():
    bot.run()


if __name__ == '__main__':

    bot_process = Process(target=run_bot)
    bot_process.start()

    app.run(port=8080, host='127.0.0.1')

    bot_process.terminate()
