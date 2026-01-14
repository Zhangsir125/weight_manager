import os
import re
import sys
import json
import random
import configparser
import numpy as np
import pandas as pd

from diet_recom.src.dish_recommendation import PregnancyDietRecommender, PregnancyInfo, weekly_diet_plan_to_json, \
    diet_parsing

sys.path.append(os.path.dirname(os.path.abspath(__file__)))
print(os.path.dirname(os.path.abspath(__file__)))
from typing import Dict, List
from sqlalchemy import create_engine
from dataclasses import dataclass
from sport_recom.src import sport_recommendation
from diet_recom.src.recommend import NutritionRecommender
from flask import Flask, request, abort, Response, jsonify
from diet_recom.src.americanRecommendedData import UserData, RecipeData, DatasetGenerator

# 把当前文件所在文件夹的父文件夹路径加入到PYTHONPATH
abs_path = os.path.dirname(os.path.abspath(__file__))
data_dir = os.path.join(abs_path, "./nutrition_data")
os.makedirs(data_dir, exist_ok=True)
user_file = os.path.join(data_dir, "users.json")
recipe_file = os.path.join(data_dir, "recipes.json")
nutritious_recipes_file = os.path.join(data_dir, "nutritious_recipes.json")
model_file = os.path.join(data_dir, "nutrition_recommender.pth")
users, recipes, nutritious_recipes_dicts = DatasetGenerator.load_from_file(user_file, recipe_file,
                                                                           nutritious_recipes_file)

app = Flask(__name__)


@dataclass
class UserWbeData:
    """用户基础数据类"""
    userId: int
    sex: int
    age: int
    height: float
    weight: float
    preweight: float
    gestational_weeks: int
    input_day: float
    consum_day: float
    activity_factor: float

    def to_dict(self) -> Dict:
        return {
            "userId": self.userId,
            "age": self.age,
            "height": self.height,
            "weight": self.weight,
            "preweight": self.preweight,
            "gestational_weeks": self.gestational_weeks,
            "input_day": self.input_day,
            "consum_day": self.consum_day,
            "activity_factor": self.activity_factor
        }


@dataclass
class Recipe:
    """食谱数据类"""
    serialNumber: int
    dishName: str
    ingredients: str
    dishType: str
    nutritionID: int
    kcal_per_100g: int
    fooDportion: str


@dataclass
class NutritionSummary:
    """营养汇总数据类"""
    recipe_id: int
    nutritionID: int
    energy: int
    protein: float
    calcium: float
    iron: float
    folate: float


class RecipeService:

    @classmethod  # 根据营养汇总信息列表生成每日食谱列表
    def generate_daily_recipes(cls, nutritionSummary: List[NutritionSummary]) -> List[Recipe]:
        nutritionIDlist = [nutrition.nutritionID for nutrition in nutritionSummary]
        params = ", ".join(["%s"] * len(nutritionIDlist))  # 避免字符串格式化漏洞
        # select_query = f"SELECT * FROM my_h_nutritional_recipes WHERE nutritionID IN ({params})"
        select_query = f'''SELECT 
                            dish.serialNumber AS serialNumber,
                            dish.dishName AS dishName,
                            dish.ingredients AS ingredients,
                            dish.dishType AS dishType,
                            dish.nutritionID AS nutritionID,
                            cal.kcal_per_100g AS kcal_per_100g
                        FROM
                        (SELECT 
                            serialNumber,
                            dishName,
                            ingredients,
                            dishType,
                            nutritionID	
                        FROM my_h_nutritional_recipes 
                        WHERE nutritionID IN ({params})) dish
                        LEFT JOIN my_h_dish_calories cal
                        ON dish.dishName = cal.dishName'''
        df = safe_read_by_ids(select_query, nutritionIDlist)
        recipeList = df.apply(lambda row: Recipe(
            int(row["serialNumber"]),
            row["dishName"],
            row["ingredients"],
            row["dishType"],
            row["nutritionID"],
            row["kcal_per_100g"],
            fooDportion=''
        ), axis=1).tolist()
        for r in recipeList:
            total = sum(extract_number(u) for u in r.ingredients.split(",") if has_digit(u))
            total = total // 10 * 10 + 10 if total > 50 else 50
            r.fooDportion = f"{total}g"
        # for recipe in recipeList:
        #     pass
        # for nutritionSummary in nutritionSummary:
        #      for recipe in recipeList:
        #          if nutritionSummary.nutritionID==recipe.nutritionID:
        #              nutritionSummary.recipeList.append(recipe)
        return recipeList

    @classmethod  # 根据用户数据计算并返回营养汇总信息
    def calculate_nutrition(cls, user_data: UserWbeData) -> List[NutritionSummary]:
        userdata = UserData(user_data.userId, user_data.sex, user_data.gestational_weeks,
                            user_data.height, user_data.weight, user_data.preweight, user_data.age,
                            user_data.activity_factor)

        recommended_recipes = recommender.recommend(userdata, recipes, top_k=20)
        nutritionList = [NutritionSummary(i, int(recommended_recipes[i].name[3:]), recommended_recipes[i].energy,
                                          recommended_recipes[i].protein, recommended_recipes[i].calcium,
                                          recommended_recipes[i].iron, recommended_recipes[i].folate) for i in
                         np.random.choice(range(20), 7, replace=False)]
        return nutritionList


class ResponseBuilder:
    """响应构建类，负责构建统一的API响应格式"""

    @staticmethod
    def build_success_response(user_data: UserWbeData, recipes: List[Recipe],
                               nutrition: List[NutritionSummary]) -> Dict:
        # serialNumber: int
        # dishName: str
        # ingredients: str
        # dishType: str
        # nutritionID: int
        recipe_list = [{
            "serialNumber": r.serialNumber,
            "dishName": r.dishName,
            "ingredients": r.ingredients,
            "dishType": r.dishType,
            "nutritionID": r.nutritionID,
            "fooDportion": r.fooDportion,
            "kcal_per_100g": r.kcal_per_100g,
        } for r in recipes]

        nutrition_List = [{
            "energy": nu.energy,
            "nutritionID": nu.nutritionID,
            "protein": nu.protein,
            "calcium": nu.calcium,
            "iron": nu.iron,
            "folate": nu.folate,
        } for nu in nutrition]
        user_dict = {
            "user_id": user_data.userId,
            # "user_bmi":deicisionResult.bmi,
            # "user_bmi_category":deicisionResult.bmi_category,
            # "weight_gain":deicisionResult.weight_gain,
            # "weight_range":deicisionResult.weight_range,
            # "predicted_percentage":deicisionResult.predicted_percentage,
            # "status_code":deicisionResult.status_code,
            # "status": deicisionResult.status,

        }

        return {
            "code": 200,
            "status": "success",
            "data": {
                "user_info": user_dict,
                "nutritions": nutrition_List,
                "recipes": recipe_list
            }
        }

    @staticmethod
    def build_error_response(message: str) -> Dict:
        """构建错误响应"""
        return {
            "status": "error",
            "message": message
        }


# 定义一个白名单
allowed_ips = []


def ip_whitelist(f):
    def wrapper(*args, **kwargs):
        if request.remote_addr in allowed_ips:
            return f(*args, **kwargs)
        else:
            # 在网页中主动抛出错误
            abort(403)

    return wrapper


def get_pregnancy(week):
    if week <= 12:
        return '孕早期'
    elif week < 28:
        return '孕中期'
    else:
        return '孕晚期'


def safe_read_by_ids(select_query, ids):
    config = read_ini("conf/app.ini")
    host = config.get("mysql", "host")
    port = config.get("mysql", "port")
    username = config.get("mysql", "username")
    password = config.get("mysql", "password")
    database = config.get("mysql", "database")
    # print(f'mysql+pymysql://{username}:{password}@{host}:{port}/{database}')
    engine = create_engine(f'mysql+pymysql://{username}:{password}@{host}:{port}/{database}')
    if ids == 0:
        df = pd.read_sql(select_query, engine)
    else:
        df = pd.read_sql(select_query, engine, params=tuple(ids))
    return df


def read_ini(filename: str = "conf/app.ini"):
    """
    Read configuration from ini file.
    :param filename: filename of the ini file
    """
    config = configparser.ConfigParser()
    if not os.path.exists(filename):
        raise FileNotFoundError(f"File {filename} not found")
    config.read(filename, encoding="utf-8")
    return config


def extract_number(text):
    """从字符串中提取数字部分（支持整数和小数）"""
    match = re.search(r'\d+(?:\.\d+)?', text)
    return float(match.group()) if match else None


def has_digit(text):
    return bool(re.search(r'\d', text))


def sample_80_percent(my_list):
    """
    从列表中随机抽取约80%的元素

    参数:
        my_list: 要抽取元素的列表

    返回:
        包含约80%元素的随机子列表
    """
    # 处理空列表情况
    if not my_list:
        return []

    # 计算需要抽取的元素数量，确保至少抽取1个元素（当列表长度较小时）
    list_length = len(my_list)
    sample_size = round(list_length * 0.8)
    # 确保抽取数量在合理范围内（1到列表长度之间）
    sample_size = max(1, min(sample_size, list_length))

    # 随机抽取元素
    return random.sample(my_list, sample_size)


@app.route('/sport', methods=['GET', 'POST'])
# @ip_whitelist
def sport():
    if request.method == 'GET':
        return '欢迎来到主页！'
    elif request.method == 'POST':
        usermes = request.json.get('userMes')
        try:
            select_query = "select * from my_h_exercise_intensity"
            df = safe_read_by_ids(select_query, 0)
            # df = pd.read_excel('./data/运动强度.xlsx')
        except Exception as e:
            err = {"code": 1045,
                   "status": "error",
                   "error": f"{e}"}
            return Response(json.dumps(err, ensure_ascii=False, sort_keys=False), mimetype='application/json')
            #  孕周
        week = usermes.get('week')
        pregnancy = get_pregnancy(week)

        trimesters = {
            "孕早期": 1,
            "孕中期": 2,
            "孕晚期": 3
        }
        # 示例：为不同孕期生成运动计划 [1,2,3]
        trimester = trimesters.get(pregnancy)
        weekly_calorie_deficits = usermes.get('weekly_calorie_deficits')
        weight_kg = usermes.get('weight_kg')
        # 用户偏好的运动列表
        user_preferences = usermes.get('user_preferences')
        # 休息日
        rest_days = usermes.get('rest_days')
        planned_days = usermes.get('planned_days') if usermes.get('planned_days') else 7
        plan = {}
        try:
            # 创建运动计划生成器
            planner = sport_recommendation.ExercisePlanner(df)
            plan = planner.generate_weekly_plan(
                trimester=trimester,
                weekly_calorie_deficit=weekly_calorie_deficits,
                weight_kg=weight_kg,
                rest_days=rest_days,
                planned_days=planned_days,
                preferred_exercises=user_preferences
            )
            plan["pregnancy"] = pregnancy
            plan["code"] = 200
            plan["status"] = "success"
        except Exception as e:
            plan = {"code": 400,
                    "status": "error",
                    "error": f"{e}"}
        finally:
            return Response(json.dumps(plan, ensure_ascii=False, sort_keys=False), mimetype='application/json')
    else:
        abort(405)


@app.route('/diet', methods=['GET', 'POST'])
# @ip_whitelist
def diet():
    if request.method == 'GET':
        return '欢迎来到主页！'
    elif request.method == 'POST':
        usermes = request.json.get('userMes')
        try:
            select_query = "select * from my_h_dish_classify_meal"
            dishes = safe_read_by_ids(select_query, 0)
            # dishes = pd.read_excel("./get_nutrition_data/dish_classify_meal_all.xlsx")
        except Exception as e:
            err = {"code": 1045,
                   "status": "error",
                   "error": f"{e}"}
            return Response(json.dumps(err, ensure_ascii=False, sort_keys=False), mimetype='application/json')
            #  孕周
        week = usermes.get('week')
        bmi = usermes.get('bmi')
        pre_pregnancy_weight = usermes.get('pre_pregnancy_weight')
        pregnancy_weight = usermes.get('pregnancy_weight')
        activity_level = usermes.get('activity_level')
        pregnancy = get_pregnancy(week)

        trimesters = {
            "孕早期": 1,
            "孕中期": 2,
            "孕晚期": 3
        }
        # 示例：为不同孕期生成运动计划 [1,2,3]
        trimester = trimesters.get(pregnancy)
        weekly_plan = {}
        try:
            dishes = diet_parsing(dishes)
            #  增加数据随机性
            sample_80_dishes = sample_80_percent(dishes)
            # 初始化推荐器
            recommender = PregnancyDietRecommender(sample_80_dishes)
            # 输入孕妇信息（示例：BMI22.5，孕中期，孕前60kg，轻度活动）
            pregnant_info = PregnancyInfo(
                bmi=bmi,
                week=week,
                trimester=trimester,
                pre_pregnancy_weight=pre_pregnancy_weight,
                pregnancy_weight=pregnancy_weight,
                activity_level=activity_level
            )

            planned_days = usermes.get('planned_days') if usermes.get('planned_days') else 7
            # 生成7天计划（可修改days参数调整天数）
            weekly_plan = recommender.recommend_multi_day_plan(pregnant_info, days=planned_days)
            weekly_plan = weekly_diet_plan_to_json(weekly_plan)

            weekly_plan["code"] = 200
            weekly_plan["status"] = "success"
        # except ReferenceError as refe:
        #     weekly_plan = {"code": 300,
        #                    "status": "error",
        #                    "error": f"{refe}"}
        except Exception as e:
            weekly_plan = {"code": 400,
                           "status": "error",
                           "error": f"{e}"}
        finally:
            return Response(json.dumps(weekly_plan, ensure_ascii=False, sort_keys=False), mimetype='application/json')
    else:
        abort(405)


@app.route('/api/recipe/recommend', methods=['POST'])
def recommend():
    """推荐食谱API接口"""
    try:
        # 1. 接收并验证参数
        data = request.get_json()

        # 2. 创建用户数据对象
        try:
            user_data = UserWbeData(
                userId=int(data['user_id']),
                sex=int(data['sex']),
                age=int(data['age']),
                height=float(data['height']),
                weight=float(data['weight']),
                preweight=float(data['preweight']),
                gestational_weeks=int(data['gestational_weeks']),
                input_day=float(data['input_day']),
                consum_day=float(data['consum_day']),
                activity_factor=data['activity_factor']
            )
        except (ValueError, TypeError) as e:
            return jsonify(ResponseBuilder.build_error_response(f"Invalid parameter type: {str(e)}")), 400
        print("Decision Result:")
        nutrition = RecipeService.calculate_nutrition(user_data)
        recipes = RecipeService.generate_daily_recipes(nutrition)
        # 5. 构建并返回响应
        response = ResponseBuilder.build_success_response(user_data, recipes, nutrition)
        return jsonify(response), 200

    except Exception as e:
        # 处理未知错误
        return jsonify(ResponseBuilder.build_error_response(f"Internal server error: {str(e)}")), 500


@app.route('/health')
def health():
    return Response(json.dumps({'status': 'UP'}), mimetype='application/json')


if __name__ == '__main__':
    recommender = NutritionRecommender()
    recommender.load_model(model_file)
    app.run(debug=False, host='0.0.0.0', port=5000)
